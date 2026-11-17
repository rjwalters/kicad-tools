#!/usr/bin/env bash
# test-champion-merge-risk-self-release.sh - tests for Champion's merge-risk
# self-release (#12950): the `champion.mergeRisk.selfReleaseConfidence` key,
# the deterministic kept-set check that runs before any confidence, and the
# doc surfaces that must mention them.
#
# champion-pr-merge.md is prose an LLM executes, so this extracts the shipped
# `self_release_threshold`, `self_release_clears`, `kept_set_hit` and
# `KEPT_SET_PATTERNS` from it and drives them against stub `loom-daemon` / `gh`.
#
# Usage:
#   ./defaults/scripts/tests/test-champion-merge-risk-self-release.sh

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
SHADOW_MD="$PROMPT_DIR/champion-merge-risk-shadow.md"

# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"

SRC="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -f "$SRC"; rm -rf "$STUB_DIR"' EXIT

# One definition of each, extracted verbatim from the prompt.
awk '/^KEPT_SET_PATTERNS=\(/{f=1} f{print} f&&/^\)/{exit}' "$CHAMPION_MD" >"$SRC"
for fn in self_release_threshold self_release_clears kept_set_hit; do
    awk -v fn="$fn" '$0 ~ "^"fn"\\(\\) \\{"{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD" >>"$SRC"
done
for sym in 'KEPT_SET_PATTERNS=(' 'self_release_threshold() {' 'self_release_clears() {' 'kept_set_hit() {'; do
    grep -qF "$sym" "$SRC" && pass "$sym is shipped in champion-pr-merge.md" \
        || fail "$sym is shipped in champion-pr-merge.md"
done

# $1 = loom-daemon stub stdout, $2 = its exit code; $3 = confidence; prints
# "clear" or "hold".
clears() {
    printf '#!/usr/bin/env bash\nprintf "%%s" %q\nexit %s\n' "$1" "$2" >"$STUB_DIR/loom-daemon"
    chmod +x "$STUB_DIR/loom-daemon"
    PATH="$STUB_DIR:$PATH" bash -c "$(cat "$SRC")"'
if self_release_clears '"'$3'"'; then echo clear; else echo hold; fi'
}
thr() {
    printf '#!/usr/bin/env bash\nprintf "%%s" %q\nexit %s\n' "$1" "$2" >"$STUB_DIR/loom-daemon"
    chmod +x "$STUB_DIR/loom-daemon"
    PATH="$STUB_DIR:$PATH" bash -c "$(cat "$SRC")"'
self_release_threshold'
}

K='"key":"champion.mergeRisk.selfReleaseConfidence"'
assert_eq 0.9 "$(thr "{$K,\"found\":false,\"value\":null}" 1)" "key unset -> 0.9"
assert_eq 0.8 "$(thr "{$K,\"found\":true,\"value\":0.8}" 0)" "0.8 -> 0.8"
assert_eq 0.9 "$(thr "{$K,\"found\":true,\"value\":\"0.5\"}" 0)" "string -> 0.9"
assert_eq 0.9 "$(thr "{$K,\"found\":true,\"value\":0}" 0)" "0 (outside (0,1]) -> 0.9"
assert_eq 0.9 "$(thr "{$K,\"found\":true,\"value\":1.5}" 0)" "1.5 (outside (0,1]) -> 0.9"
assert_eq 0.9 "$(thr 'not json' 0)" "garbage -> 0.9"
assert_eq 0.9 "$(thr '' 2)" "verb failure -> 0.9"

UNSET="{$K,\"found\":false,\"value\":null}"
assert_eq clear "$(clears "$UNSET" 1 0.93)" "0.93 >= default 0.9 -> self-release"
assert_eq clear "$(clears "$UNSET" 1 0.9)" "0.9 == default 0.9 -> self-release"
assert_eq hold  "$(clears "$UNSET" 1 0.89)" "0.89 < default 0.9 -> hold"
assert_eq hold  "$(clears "$UNSET" 1 abc)" "non-numeric confidence -> hold"
assert_eq hold  "$(clears "$UNSET" 1 1.5)" "confidence above 1 -> hold"
assert_eq hold  "$(clears "$UNSET" 1 '')" "empty confidence -> hold"
assert_eq hold  "$(clears "{$K,\"found\":true,\"value\":1.0}" 0 1.0)" "threshold 1.0 disables self-release even at 1.0"
assert_eq clear "$(clears "{$K,\"found\":true,\"value\":0.7}" 0 0.75)" "lowered threshold 0.7 admits 0.75"

# Kept set: a stubbed `gh` prints the changed-file list.
hit() {
    printf '#!/usr/bin/env bash\nprintf "%%s" %q\nexit %s\n' "$1" "${2:-0}" >"$STUB_DIR/gh"
    chmod +x "$STUB_DIR/gh"
    PATH="$STUB_DIR:$PATH" bash -c "$(cat "$SRC")"'
if kept_set_hit 7; then echo hold; else echo free; fi'
}
assert_eq free "$(hit $'docs/guide.md\nREADME.md')" "docs-only PR touches no kept-set path"
assert_eq hold "$(hit $'docs/guide.md\ndefaults/scripts/merge-pr.sh')" "merge gate path -> kept set"
assert_eq hold "$(hit 'loom-daemon/src/tokens.rs')" "tokens -> kept set"
assert_eq hold "$(hit 'defaults/hooks/guard-destructive.sh')" "guard engine -> kept set"
assert_eq hold "$(hit 'scripts/install-loom.sh')" "installer -> kept set"
assert_eq hold "$(hit 'loom-daemon/src/auto_update.rs')" "roll/updater -> kept set"
assert_eq hold "$(hit 'loom-daemon/src/deep_clean.rs')" "destructive cleanup -> kept set"
assert_eq hold "$(hit 'loom-daemon/src/credential_preflight.rs')" "credential binding -> kept set"
assert_eq hold "$(hit 'db/migrations/0001.sql')" "migrations -> kept set"
assert_eq hold "$(hit '' 0)" "empty file list fails closed"
assert_eq hold "$(hit 'docs/a.md' 1)" "unreadable file list fails closed"

echo "--- Doc pins ---"
for f in "$CHAMPION_MD" "$CHAMPION_REF_MD"; do
    if grep -q 'champion.mergeRisk.selfReleaseConfidence' "$f" && grep -q 'champion:merge-risk-self-release' "$f"; then
        pass "$(basename "$f") documents the key and the marker"
    else
        fail "$(basename "$f") documents the key and the marker"
    fi
done
grep -q 'park-record decision' "$CHAMPION_MD" && pass "prompt exports confidence and outcome" \
    || fail "prompt exports confidence and outcome"
grep -q 'not the confidence\|not the shadow score\|Not the shadow score\|also \*\*not\*\* the confidence' "$SHADOW_MD" \
    && pass "shadow doc says the score is not the confidence" \
    || fail "shadow doc says the score is not the confidence"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
