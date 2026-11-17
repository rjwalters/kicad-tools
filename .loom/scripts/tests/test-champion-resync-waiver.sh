#!/usr/bin/env bash
# test-champion-resync-waiver.sh - Regression tests for the Champion resync
# waiver (#13096): a `chore(loom): resync installed Loom to vX` PR passes the
# merge-risk kept-set hold only when every changed kept-set file has the same
# blob SHA as its defaults/ counterpart at tag vX, and holds on anything else.
#
# The rules live once, in loom-daemon (fleet_sync/workspace_resync/waiver.rs,
# unit-tested there; #13487). champion-pr-merge.md is prose an LLM executes, so
# this extracts the real resync_waiver_eval wrapper from it and runs it against
# a stubbed `loom-daemon forge resync-waiver`: only an exit-0 ELIGIBLE answer
# with a tag and a record passes, everything else fails closed.
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
awk '/^resync_waiver_eval\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD" >"$SRC"
if grep -q "resync_waiver_eval" "$SRC"; then pass "extracted resync_waiver_eval from champion-pr-merge.md"; else fail "could not extract resync_waiver_eval"; fi
assert_not_contains "$(cat "$SRC")" "gh api" "the wrapper holds no second copy of the rules (no forge reads)"

# --- loom-daemon stub -----------------------------------------------------------
STUB="$WORK/loom-daemon"
cat >"$STUB" <<'STUBEOF'
#!/usr/bin/env bash
[ "$1 $2" = "forge resync-waiver" ] || exit 2
[ -f "$FX/stdout" ] && cat "$FX/stdout"
exit "$(cat "$FX/rc" 2>/dev/null || echo 0)"
STUBEOF
chmod +x "$STUB"

new_fx() { FX="$(mktemp -d "$WORK/fx.XXXXXX")"; }
# run_eval [bin]: prints "<RESULT>|<TAG>|<RECORD>"
run_eval() {
    FX="$FX" LOOM_DAEMON_BIN="${1:-$STUB}" bash -c '
        set -u
        PR_NUMBER=7
        source "'"$SRC"'"
        resync_waiver_eval
        printf "%s|%s|%s" "$RESYNC_WAIVER_RESULT" "$RESYNC_WAIVER_TAG" "$RESYNC_WAIVER_RECORD"
    '
}
result_of() { printf '%s' "${1%%|*}"; }

echo "--- an ELIGIBLE answer ---"
new_fx
printf 'RESYNC_WAIVER=ELIGIBLE\nRESYNC_WAIVER_TAG=v0.19.1209\nRESYNC_WAIVER_RECORD=.loom/scripts/spawn-claude.sh: aaa111\nRESYNC_WAIVER_RECORD=.loom/scripts/guard-foo.sh: bbb222\n' >"$FX/stdout"
out="$(run_eval)"
assert_eq "ELIGIBLE" "$(result_of "$out")" "an exit-0 ELIGIBLE answer passes the kept-set hold"
assert_contains "$out" "|v0.19.1209|" "the tag is carried through"
assert_contains "$out" ".loom/scripts/spawn-claude.sh: aaa111" "the record lists each file with its blob SHA"
assert_contains "$out" ".loom/scripts/guard-foo.sh: bbb222" "the record lists every kept-set file"

echo "--- everything else fails closed ---"
new_fx; printf 'RESYNC_WAIVER=NOT_ELIGIBLE\n' >"$FX/stdout"; echo 1 >"$FX/rc"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a NOT_ELIGIBLE answer holds"
new_fx; printf 'RESYNC_WAIVER=ELIGIBLE\nRESYNC_WAIVER_TAG=v1.2.3\nRESYNC_WAIVER_RECORD=a: b\n' >"$FX/stdout"; echo 1 >"$FX/rc"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "ELIGIBLE text with a non-zero exit holds"
new_fx; printf 'RESYNC_WAIVER=ELIGIBLE\n' >"$FX/stdout"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "ELIGIBLE without a tag or record holds"
new_fx; printf 'something else\n' >"$FX/stdout"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "unrecognised output holds"
new_fx
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval "$WORK/no-such-daemon")")" "a missing binary holds"
new_fx; echo 2 >"$FX/rc"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a daemon that predates the verb (clap exit 2) holds"

# The #13486 (tests/ exemption) and #13435 (Repo Skills) cases now run against the native proof:
# see fleet_sync/tests/workspace_resync_waiver.rs.

echo "--- docs ---"
assert_doc() { assert_contains "$(cat "$1")" "$2" "$3"; }
assert_doc "$CHAMPION_MD" "Resync waiver for the kept-set hold (#13096)" "champion-pr-merge.md documents the waiver"
assert_doc "$CHAMPION_MD" "champion:resync-waiver tag=" "pre-merge comment carries a greppable waiver marker"
assert_doc "$CHAMPION_MD" "KEPT_SET_PATTERNS\` stays hardcoded" "doc states the floor stays hardcoded"
assert_doc "$CHAMPION_MD" "compared byte-for-byte like any kept-set file (#13486)" "champion-pr-merge.md documents the tests/ exemption"
assert_doc "$CHAMPION_REF_MD" "Bump Repo Skills <from> -> <X.Y.Z>" "champion-reference.md documents the Repo Skills variant"
assert_doc "$CHAMPION_REF_MD" "Resync waiver (#13096)" "champion-reference.md documents the waiver"
assert_doc "$CHAMPION_MD" "Step 3b: Close Superseded Resync PRs" "champion-pr-merge.md closes superseded resyncs"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
