#!/usr/bin/env bash
# test-champion-ratification-hold.sh - runs the SHIPPED ratification-hold tick
# (#12992).
#
# Extracts the one ```bash block from champion-ratification-hold.md and executes
# it unchanged against stub `gh`, `loom-daemon` and `post-comment.sh`, asserting
# the forge writes each `action=` makes. The detector, the 2026-10-20 fixtures,
# the release rules and the state machine are unit-tested in
# loom-daemon/src/champion_hold/ratification/tests.rs; this file pins the tick
# side and the prompt wiring: every action's writes, fail-closed handling of a
# failed verb, and that nothing in the hold consults a standing authorization.
#
# Usage: ./.loom/scripts/tests/test-champion-ratification-hold.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
HOLD_MD="$PROMPT_DIR/champion-ratification-hold.md"
MERGE_MD="$PROMPT_DIR/champion-pr-merge.md"

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
PR=4242
HEAD_SHA=0123456789abcdef0123456789abcdef01234567

TESTS_RUN=0
TESTS_FAILED=0
check() { # <expected> <actual> <message>
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$1" == "$2" ]]; then
        echo "  PASS: $3"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo "  FAIL: $3"
        echo "    expected: '$1'"
        echo "    actual:   '$2'"
    fi
}

awk '/^```bash$/{f=1;next} /^```$/{f=0} f' "$HOLD_MD" | sed "s/^PR_NUMBER=<number>$/PR_NUMBER=$PR/" >"$T/tick.sh"
grep -q "^PR_NUMBER=$PR$" "$T/tick.sh" || { echo "FAIL: could not extract the tick from $HOLD_MD"; exit 1; }

mkdir -p "$T/bin" "$T/w/.loom/scripts"
cat >"$T/bin/gh" <<EOF
#!/usr/bin/env bash
S="$T"
case "\$1 \$2" in
  "pr edit")
    if [ "\$4" = --add-label ]; then echo "LABEL+:\$5" >>"\$S/writes"; else echo "LABEL-:\$5" >>"\$S/writes"; fi ;;
  "pr view") cat "\$S/other_hold" ;;
esac
exit 0
EOF
cat >"$T/bin/loom-daemon" <<EOF
#!/usr/bin/env bash
S="$T"
case "\$1 \$2" in
  "champion-hold ratification")
    cat "\$S/verb_out"; exit "\$(cat "\$S/verb_rc")" ;;
  "park-record apply")
    echo "PARK:\$*" | sed 's/--pr [0-9]* //' >>"\$S/writes" ;;
esac
EOF
cat >"$T/w/.loom/scripts/post-comment.sh" <<EOF
#!/usr/bin/env bash
S="$T"
body=\$4
echo "COMMENT:\$(printf '%s\n' "\$body" | head -1 | sed 's/<!-- champion:\(.*\) -->/\1/')" >>"\$S/writes"
printf '%s\n' "\$body" >"\$S/last_body"
EOF
chmod +x "$T/bin/gh" "$T/bin/loom-daemon" "$T/w/.loom/scripts/"*.sh

tick() { # <action|-> <rc> [files] [other_hold]
    if [[ "$1" == "-" ]]; then : >"$T/verb_out"; else
        echo "LOOM-CHAMPION-RATIFICATION verdict=hold action=$1 pr=$PR head=$HEAD_SHA files=${3:-spec/spec.md,spec/decisions/0001.md}" >"$T/verb_out"
    fi
    echo "$2" >"$T/verb_rc"; echo "${4:-false}" >"$T/other_hold"
    : >"$T/writes"; rm -f "$T/last_body"
    OUT=$(cd "$T/w" && env -u LOOM_DAEMON_BIN PATH="$T/bin:$PATH" bash -c 'source "$1"; echo "SKIP=${SKIP_REMAINING:-false}"' _ "$T/tick.sh" 2>&1)
    SKIP=$(sed -n 's/^SKIP=//p' <<<"$OUT")
    WRITES=$(paste -sd';' "$T/writes")
}
PARK='PARK:park-record apply --reason champion ratification hold --by champion'

echo "--- every action's forge writes ---"
tick none 0;    check "" "$WRITES" "none: no writes";                       check false "$SKIP" "none: the other criteria run"
tick hold 0;    check "COMMENT:ratification-hold;$PARK" "$WRITES" "hold: notice and park record (apply adds loom:blocked)"
check true "$SKIP" "hold: ends this PR's pass"
check 1 "$(grep -c 'merging ratifies; the operator approves' "$T/last_body" || true)" "hold: notice says merging ratifies; the operator approves"
check 1 "$(grep -c 'clear the hold' "$T/last_body" || true)" "hold: notice names the release comment"
check 1 "$(grep -c 'spec/spec.md' "$T/last_body" || true)" "hold: notice lists the declaring files"
tick stands 0;  check "$PARK" "$WRITES" "stands: park re-asserted via apply, no comment"; check true "$SKIP" "stands: ends this PR's pass"
tick release 0; check "COMMENT:ratification-release-respected;LABEL-:loom:blocked" "$WRITES" "release: marker, label removed"; check false "$SKIP" "release: continues to the other criteria"
tick clear 0 - false; check "COMMENT:ratification-hold-cleared;LABEL-:loom:blocked" "$WRITES" "clear: marker, label removed"
tick clear 0 - true;  check "COMMENT:ratification-hold-cleared" "$WRITES" "clear: label kept while another Champion hold stands"
tick defer 0;   check "" "$WRITES" "defer: no writes"; check true "$SKIP" "defer: ends this PR's pass (no merge)"

echo "--- a failed verb fails closed ---"
tick - 3;  check "" "$WRITES" "exit 3: no writes";  check true "$SKIP" "exit 3: no merge this pass"
tick - 0;  check "" "$WRITES" "empty stdout: no writes"; check true "$SKIP" "empty stdout: no merge this pass"
tick - 2;  check "" "$WRITES" "exit 2 (old daemon): no writes"; check false "$SKIP" "exit 2: merge-pr.sh still refuses a held PR"

echo "--- the hold is never waivable ---"
tick_src=$(cat "$T/tick.sh")
check 0 "$(grep -c 'standing_auth\|standingAuthorization\|auto-merge-ok' <<<"$tick_src" || true)" "the tick never consults a standing authorization or the auto-merge-ok override"
check 1 "$(grep -c 'standing_auth_eval' "$HOLD_MD" | awk '$1>=1{print 1}')" "the prompt states standing_auth_eval is not consulted"
check 1 "$(grep -c '### `ratification`: Ratification Hold' "$MERGE_MD")" "champion-pr-merge.md wires the hold in as ratification"
check 1 "$(grep -c 'champion-ratification-hold.md' "$MERGE_MD" | awk '$1>=1{print 1}')" "champion-pr-merge.md points at the hold prompt"
# merge-risk/critical-files run standing_auth_eval before ratification; it sits after critical-files and before mergeable.
a=$(grep -n '^### `critical-files`: Critical File' "$MERGE_MD" | cut -d: -f1); b=$(grep -n '^### `ratification`: Ratification' "$MERGE_MD" | cut -d: -f1); c=$(grep -n '^### `mergeable`: Merge Conflict' "$MERGE_MD" | cut -d: -f1)
check yes "$([[ -n "$a" && "$a" -lt "$b" && "$b" -lt "$c" ]] && echo yes || echo no)" "ratification sits between critical-files and mergeable"

echo
echo "$((TESTS_RUN - TESTS_FAILED))/$TESTS_RUN passed"
[[ "$TESTS_FAILED" -eq 0 ]]
