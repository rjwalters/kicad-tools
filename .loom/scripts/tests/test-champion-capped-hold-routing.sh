#!/usr/bin/env bash
# test-champion-capped-hold-routing.sh - #11905: the Capped-PR Recovery Pass
# routes a Champion-held PR at the Doctor-cycle cap to the operator inbox.
#
# Extract-and-execute (#7979): the SHIPPED bash block that follows "Route it
# to the operator inbox once" in champion-pr-merge.md is run with `gh`,
# `$GH_READ` and `post-comment.sh` stubbed. It must post the
# marker-guarded recommendation and add `loom:operator` once, never touch
# `loom:blocked`, and do nothing when the marker already stands (a human may
# have removed `loom:operator` since; only a human removes it).
#
# Hermetic: no network, no forge, no tokens.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-capped-hold-routing.sh

# SC2034: PR_NUMBER / GH_READ are read by the eval'd shipped block.
# SC2016: backticks in the asserted literals are Markdown, not expansions.
# shellcheck disable=SC2034,SC2016

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
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
assert_contains() {
    if grep -qF -- "$2" <<<"$1"; then pass "$3"; else fail "$3 (missing '$2' in: $1)"; fi
}
assert_lacks() {
    if grep -qF -- "$2" <<<"$1"; then fail "$3 (unexpected '$2' in: $1)"; else pass "$3"; fi
}

BLOCK=$(awk '
    /Route it to the operator inbox once/ { armed = 1; next }
    armed && /^```bash/ { inside = 1; next }
    inside && /^```/ { exit }
    inside { print }
' "$CHAMPION_MD")

echo "Test 1: the shipped block exists and is marker-guarded"
if [[ -n "$BLOCK" ]]; then pass "block extracted"; else fail "block not found in $CHAMPION_MD"; fi
assert_contains "$BLOCK" '<!-- champion:capped-hold-recommendation -->' "guarded by the recommendation marker"
assert_contains "$BLOCK" '--add-label "loom:operator"' "adds loom:operator"
assert_lacks "$BLOCK" '--remove-label' "never removes a label"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/.loom/scripts"
cat >"$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >>"$CALLS"
EOF
cat >"$WORK/.loom/scripts/post-comment.sh" <<'EOF'
#!/usr/bin/env bash
echo "post-comment $1 $2" >>"$CALLS"
printf '%s\n' "$4" >"$BODY_FILE"
EOF
cat >"$WORK/bin/gh-read" <<'EOF'
#!/usr/bin/env bash
echo "$MARKER_STANDS"
EOF
chmod +x "$WORK/bin/gh" "$WORK/bin/gh-read" "$WORK/.loom/scripts/post-comment.sh"

run_block() {
    export CALLS="$WORK/calls.log" BODY_FILE="$WORK/body.txt" MARKER_STANDS="$1"
    rm -f "$CALLS" "$BODY_FILE"
    (
        cd "$WORK" || exit 1
        PATH="$WORK/bin:$PATH"
        PR_NUMBER=42
        GH_READ="$WORK/bin/gh-read"
        eval "$BLOCK"
    )
    cat "$CALLS" 2>/dev/null || true
}

echo "Test 2: first pass posts the recommendation, then routes to the inbox"
OUT=$(run_block false)
assert_contains "$OUT" "post-comment 42 --pr" "posts one comment"
assert_contains "$OUT" 'gh pr edit 42 --add-label loom:operator' "adds loom:operator"
BODY=$(cat "$WORK/body.txt" 2>/dev/null || true)
if [[ "$BODY" == "<!-- champion:capped-hold-recommendation -->"* ]]; then
    pass "the comment opens with the marker"
else
    fail "the comment must open with the marker: $BODY"
fi
assert_contains "$BODY" 'remove both `loom:blocked` and `loom:operator` to release the hold' "names the release"
FIRST=$(head -n 1 <<<"$OUT")
if [[ "$FIRST" == "gh pr edit 42 --add-label loom:operator" ]]; then
    pass "the label goes on before the comment (a failed label write leaves no marker)"
else
    fail "loom:operator must be added first, got: $FIRST"
fi
assert_lacks "$OUT" "loom:blocked" "never edits loom:blocked"

echo "Test 3: the marker stands -> no comment, no label (idempotent)"
OUT=$(run_block true)
if [[ -z "$OUT" ]]; then pass "no forge writes"; else fail "expected no writes, got: $OUT"; fi

echo "Test 4: docs agree"
LSM="$PROMPT_DIR/../../../docs/label-state-machine.md"
[[ -f "$LSM" ]] || LSM="$SCRIPTS_DIR/../docs/label-state-machine.md"
if grep -qF '(the inbox, #11905)' "$LSM" 2>/dev/null; then
    pass "label-state-machine.md names the inbox routing"
else
    fail "label-state-machine.md must name the inbox routing ($LSM)"
fi

echo
echo "$TESTS_RUN tests, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
