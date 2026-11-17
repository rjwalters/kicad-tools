#!/usr/bin/env bash
# test-install-agents-md-reapply.sh — regression tests for the AGENTS.md
# marker-block splice-back functions added to install.sh's `--quick`
# reinstall stash-pop path (issue #6196).
#
# Issue #6196: give root AGENTS.md a delimited managed block with room
# above/below for repo-authored guidance, symmetric with root CLAUDE.md
# (`<!-- BEGIN/END LOOM ORCHESTRATION -->`). That symmetry already existed at
# the `loom-daemon init` scaffolding layer (its own
# `AGENTS_SECTION_START`/`AGENTS_SECTION_END` marker pair — see
# loom-daemon/src/init/scaffolding.rs) — but install.sh's `--quick` reinstall
# stash-pop reconciliation (issue #3663) only ever special-cased `.gitignore`
# and `CLAUDE.md`, not `AGENTS.md`. A HEAD-reset-then-splice-back is needed so
# a stashed AGENTS.md hunk's 3-way base lines up with the freshly written
# pointer block; without it, repo-authored prose placed outside the marker
# block risked a spurious pop conflict (or, before this fix, wasn't even
# considered for reset/reapply at all).
#
# `_emit_loom_agents_block` / `reapply_loom_agents_md_block` are pure and
# side-effect free (mirrors `_emit_loom_claude_block` /
# `reapply_loom_claude_md_block`), so they are extracted via awk (same
# pattern as test-install-reinstall-safety.sh) and exercised in an isolated
# harness rather than running the full installer end-to-end.
#
# Usage:
#   bash defaults/scripts/tests/test-install-agents-md-reapply.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# In the source checkout this file lives at defaults/scripts/tests/; the repo
# root is three levels up. This test is a source-checkout artifact and
# expects the real install.sh.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_PASSED=$((TESTS_PASSED + 1)); TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() {
  TESTS_FAILED=$((TESTS_FAILED + 1)); TESTS_RUN=$((TESTS_RUN + 1))
  echo -e "  ${RED}FAIL${NC}: $1"
  [[ -n "${2:-}" ]] && echo "$2" | sed 's/^/      /'
}

if [[ ! -f "$INSTALL_SH" ]]; then
  echo "ERROR: $INSTALL_SH not found" >&2
  exit 1
fi

# Extract a single top-level function body ("name() {" ... "}") from a file.
extract_function() {
  local func_name="$1" file="$2"
  awk -v fn="${func_name}() {" '
    $0 == fn { capture=1 }
    capture { print }
    capture && /^\}$/ { exit }
  ' "$file"
}

EMIT_FN="$(extract_function "_emit_loom_agents_block" "$INSTALL_SH")"
REAPPLY_FN="$(extract_function "reapply_loom_agents_md_block" "$INSTALL_SH")"

if [[ -z "$EMIT_FN" ]]; then
  echo "ERROR: could not extract _emit_loom_agents_block() from $INSTALL_SH" >&2
  exit 1
fi
if [[ -z "$REAPPLY_FN" ]]; then
  echo "ERROR: could not extract reapply_loom_agents_md_block() from $INSTALL_SH" >&2
  exit 1
fi

# Source both functions into this process (pure functions, no side effects
# until called).
eval "$EMIT_FN"
eval "$REAPPLY_FN"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-agents-reapply.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "== Test 1: _emit_loom_agents_block extracts the inclusive marker range =="
printf 'before\n<!-- BEGIN LOOM ORCHESTRATION (AGENTS) -->\nblock content\n<!-- END LOOM ORCHESTRATION (AGENTS) -->\nafter\n' \
  | _emit_loom_agents_block > "$TMP_DIR/emitted.txt"
EXPECTED_EMIT=$'<!-- BEGIN LOOM ORCHESTRATION (AGENTS) -->\nblock content\n<!-- END LOOM ORCHESTRATION (AGENTS) -->'
ACTUAL_EMIT="$(cat "$TMP_DIR/emitted.txt")"
if [[ "$ACTUAL_EMIT" == "$EXPECTED_EMIT" ]]; then
  pass "_emit_loom_agents_block returns exactly the begin..end range"
else
  fail "_emit_loom_agents_block returns exactly the begin..end range" "expected: [$EXPECTED_EMIT]
actual: [$ACTUAL_EMIT]"
fi

echo "== Test 2: _emit_loom_agents_block ignores CLAUDE.md's marker pair =="
printf '<!-- BEGIN LOOM ORCHESTRATION -->\nclaude content\n<!-- END LOOM ORCHESTRATION -->\n' \
  | _emit_loom_agents_block > "$TMP_DIR/emitted2.txt"
if [[ ! -s "$TMP_DIR/emitted2.txt" ]]; then
  pass "_emit_loom_agents_block does not match CLAUDE.md's marker pair"
else
  fail "_emit_loom_agents_block does not match CLAUDE.md's marker pair" "$(cat "$TMP_DIR/emitted2.txt")"
fi

echo "== Test 3: reapply_loom_agents_md_block splices the fresh block, preserving repo prose around it =="
# The post-init snapshot: what loom-daemon init just wrote (the fresh
# pointer), WITH repo-authored prose above/below the marker block -- exactly
# the #6196 scenario (a Judge documenting repo-specific orchestration
# guidance for an AGENTS.md-aware runtime).
SNAPSHOT="$TMP_DIR/postinit-snapshot.md"
printf '# Repo guidance for AGENTS.md-aware runtimes\n\nBuild with: npm run build\n\n<!-- BEGIN LOOM ORCHESTRATION (AGENTS) -->\nFRESH pointer v2\n<!-- END LOOM ORCHESTRATION (AGENTS) -->\n' \
  > "$SNAPSHOT"

# The popped working copy: HEAD-reset then the user's stashed hunk applied --
# carries the user's restored prose (identical to the snapshot's, since the
# user's edit was OUTSIDE the marker block) but the OLD committed block
# content.
mkdir -p "$TMP_DIR/target"
printf '# Repo guidance for AGENTS.md-aware runtimes\n\nBuild with: npm run build\n\n<!-- BEGIN LOOM ORCHESTRATION (AGENTS) -->\nold pointer v1\n<!-- END LOOM ORCHESTRATION (AGENTS) -->\n' \
  > "$TMP_DIR/target/AGENTS.md"

reapply_loom_agents_md_block "$TMP_DIR/target" "$SNAPSHOT"

RESULT="$(cat "$TMP_DIR/target/AGENTS.md")"
if [[ "$RESULT" == *"FRESH pointer v2"* ]]; then
  pass "fresh Loom block content is spliced in"
else
  fail "fresh Loom block content is spliced in" "$RESULT"
fi
if [[ "$RESULT" != *"old pointer v1"* ]]; then
  pass "stale Loom block content is replaced, not retained"
else
  fail "stale Loom block content is replaced, not retained" "$RESULT"
fi
if [[ "$RESULT" == *"Build with: npm run build"* ]]; then
  pass "repo-authored prose outside the marker block survives the splice"
else
  fail "repo-authored prose outside the marker block survives the splice" "$RESULT"
fi

echo "== Test 4: reapply_loom_agents_md_block is a no-op when either file lacks markers =="
mkdir -p "$TMP_DIR/target-no-markers"
printf 'hand-authored AGENTS.md, no Loom markers at all\n' > "$TMP_DIR/target-no-markers/AGENTS.md"
BEFORE="$(cat "$TMP_DIR/target-no-markers/AGENTS.md")"
reapply_loom_agents_md_block "$TMP_DIR/target-no-markers" "$SNAPSHOT"
AFTER="$(cat "$TMP_DIR/target-no-markers/AGENTS.md")"
if [[ "$BEFORE" == "$AFTER" ]]; then
  pass "markerless target file is left untouched"
else
  fail "markerless target file is left untouched" "before: [$BEFORE]
after: [$AFTER]"
fi

echo ""
echo "Ran $TESTS_RUN test(s): $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
