#!/usr/bin/env bash
# test-archive-transcripts.sh - Unit tests for defaults/scripts/archive-transcripts.sh
# (issue #3726 — opt-in session-transcript archival).
#
# Strategy: build a throwaway Claude Code projects/ tree under a temp
# CLAUDE_CONFIG_DIR and a throwaway git repo as the "source cwd", then drive the
# copier through env/config and assert on the destination layout, file modes, the
# agent-id-keyed index.json, idempotency, the CLAUDE_CONFIG_DIR-aware base, the
# gitignore-or-refuse guard, and the disabled-by-default no-op.
#
# Usage:
#   bash defaults/scripts/tests/test-archive-transcripts.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SCRIPT_DIR/../archive-transcripts.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_PASSED=$((TESTS_PASSED+1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_FAILED=$((TESTS_FAILED+1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_file()    { if [[ -f "$1" ]]; then pass "$2"; else fail "$2 (missing: $1)"; fi; }
assert_absent()  { if [[ ! -e "$1" ]]; then pass "$2"; else fail "$2 (exists: $1)"; fi; }
assert_eq()      { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1' want '$2')"; fi; }
assert_contains(){ if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (missing '$2' in: $1)"; fi; }

if [[ ! -f "$SCRIPT" ]]; then echo "ERROR: $SCRIPT not found" >&2; exit 1; fi
if ! command -v jq >/dev/null 2>&1; then echo "ERROR: jq required for these tests" >&2; exit 1; fi

# Build a fresh fixture: returns via globals REPO, BASE, DEST, SLUG, DATE.
make_fixture() {
  ROOT="$(mktemp -d "${TMPDIR:-/tmp}/att-test.XXXXXX")"
  REPO="$ROOT/myrepo"; mkdir -p "$REPO"
  git -C "$REPO" init -q; git -C "$REPO" config user.email t@t; git -C "$REPO" config user.name t
  SLUG="$(printf '%s' "$REPO" | sed 's#/#-#g')"
  BASE="$ROOT/cfgdir"
  local proj="$BASE/projects/$SLUG"
  mkdir -p "$proj/UUID1/subagents" "$proj/UUID1/tool-results"
  printf '{"timestamp":"2026-07-20T10:00:00Z","message":{"model":"claude-opus-4"}}\n' > "$proj/UUID1.jsonl"
  printf '{"timestamp":"2026-07-20T10:01:00Z","message":{"model":"claude-sonnet-4"}}\n{"timestamp":"2026-07-20T10:03:00Z"}\n' > "$proj/UUID1/subagents/agent-abc.jsonl"
  printf '{"agentType":"loom-builder","description":"Build issue #42","spawnDepth":1}\n' > "$proj/UUID1/subagents/agent-abc.meta.json"
  printf 'big tool output\n' > "$proj/UUID1/tool-results/r1.txt"
  DEST="$ROOT/archive"
  DATE="$(date +%Y-%m-%d)"
}

echo "Case 1: enabled via env — copies session + subagents + sidecars + index"
make_fixture
CLAUDE_CONFIG_DIR="$BASE" LOOM_TRANSCRIPT_ARCHIVE="$DEST" \
  bash "$SCRIPT" --source-cwd "$REPO" --issue 42 >/dev/null 2>&1
S="$DEST/myrepo/$DATE/UUID1"
assert_file "$S/UUID1.jsonl"                              "session transcript copied"
assert_file "$S/UUID1/subagents/agent-abc.jsonl"         "subagent transcript copied"
assert_file "$S/UUID1/subagents/agent-abc.meta.json"     "meta sidecar copied"
assert_file "$S/UUID1/tool-results/r1.txt"               "tool-results copied"
assert_file "$S/index.json"                              "index.json emitted"
echo ""

echo "Case 2: file/dir modes are 0600/0700"
DMODE="$(stat -f '%Lp' "$S" 2>/dev/null || stat -c '%a' "$S")"
FMODE="$(stat -f '%Lp' "$S/UUID1.jsonl" 2>/dev/null || stat -c '%a' "$S/UUID1.jsonl")"
assert_eq "$DMODE" "700" "session dir is 0700"
assert_eq "$FMODE" "600" "transcript file is 0600"
echo ""

echo "Case 3: index.json is agent-id-keyed with role/issue from sidecar"
ROLE="$(jq -r '.agents[0].role' "$S/index.json")"
ISSUE="$(jq -r '.agents[0].issue' "$S/index.json")"
AID="$(jq -r '.agents[0].agent_id' "$S/index.json")"
MODEL="$(jq -r '.agents[0].model' "$S/index.json")"
SCHEMA="$(jq -r '.schema' "$S/index.json")"
SWEEP="$(jq -r '.sweep_issue' "$S/index.json")"
assert_eq "$AID"    "agent-abc"          "index keyed by agent id"
assert_eq "$ROLE"   "loom-builder"       "role sourced from meta sidecar"
assert_eq "$ISSUE"  "42"                 "issue parsed from meta description"
assert_eq "$MODEL"  "claude-sonnet-4"    "model harvested from subagent jsonl"
assert_eq "$SCHEMA" "loom.transcript-index/v1" "schema tag present"
assert_eq "$SWEEP"  "42"                 "sweep issue recorded"
echo ""

echo "Case 4: idempotent — second run copies nothing new"
OUT="$(CLAUDE_CONFIG_DIR="$BASE" LOOM_TRANSCRIPT_ARCHIVE="$DEST" \
  bash "$SCRIPT" --source-cwd "$REPO" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
assert_contains "$OUT" "0 file(s) copied" "second run is a no-op copy"
rm -rf "$ROOT"
echo ""

echo "Case 5: disabled by default — no env, no config => no-op, no dest created"
make_fixture
CLAUDE_CONFIG_DIR="$BASE" bash "$SCRIPT" --source-cwd "$REPO" >/dev/null 2>&1
RC=$?
assert_eq "$RC" "0" "disabled path exits 0"
assert_absent "$DEST" "disabled path creates no destination"
rm -rf "$ROOT"
echo ""

echo "Case 6: CLAUDE_CONFIG_DIR override is honoured (reads overridden projects/)"
make_fixture
# Point HOME somewhere with NO projects, prove the copier reads CLAUDE_CONFIG_DIR.
EMPTY_HOME="$ROOT/emptyhome"; mkdir -p "$EMPTY_HOME/.claude/projects"
HOME="$EMPTY_HOME" CLAUDE_CONFIG_DIR="$BASE" LOOM_TRANSCRIPT_ARCHIVE="$DEST" \
  bash "$SCRIPT" --source-cwd "$REPO" >/dev/null 2>&1
assert_file "$DEST/myrepo/$DATE/UUID1/UUID1.jsonl" "read projects/ from CLAUDE_CONFIG_DIR, not \$HOME/.claude"
rm -rf "$ROOT"
echo ""

echo "Case 7: gitignore-or-refuse — dest inside a repo, not ignored => refuse"
make_fixture
INREPO="$ROOT/inrepo"; mkdir -p "$INREPO"; git -C "$INREPO" init -q
OUT="$(CLAUDE_CONFIG_DIR="$BASE" bash "$SCRIPT" --source-cwd "$REPO" --dest "$INREPO/arch" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
RC=$?
assert_contains "$OUT" "NOT gitignored" "refuses when dest is tracked-tree"
if [[ "$RC" -ne 0 ]]; then pass "refusal exits non-zero"; else fail "refusal exits non-zero (got $RC)"; fi
assert_absent "$INREPO/arch/myrepo" "refusal writes no transcripts"
echo ""

echo "Case 8: gitignore-or-refuse — dest inside a repo BUT ignored => proceeds"
printf 'arch/\n' > "$INREPO/.gitignore"
CLAUDE_CONFIG_DIR="$BASE" bash "$SCRIPT" --source-cwd "$REPO" --dest "$INREPO/arch" >/dev/null 2>&1
assert_file "$INREPO/arch/myrepo/$DATE/UUID1/UUID1.jsonl" "proceeds when dest is gitignored"
rm -rf "$ROOT"
echo ""

echo "Case 9: explicit env 'off' disables even if config would enable"
make_fixture
mkdir -p "$REPO/.loom"
printf '{"loom":{"transcriptArchive":{"enabled":true,"dir":"%s"}}}\n' "$DEST" > "$REPO/.loom/config.json"
CLAUDE_CONFIG_DIR="$BASE" LOOM_TRANSCRIPT_ARCHIVE="off" bash "$SCRIPT" --source-cwd "$REPO" >/dev/null 2>&1
assert_absent "$DEST" "env=off wins over config-enabled (env-over-config)"
echo ""

echo "Case 10: config-enabled (no env) archives to config dir"
CLAUDE_CONFIG_DIR="$BASE" bash "$SCRIPT" --source-cwd "$REPO" >/dev/null 2>&1
assert_file "$DEST/myrepo/$DATE/UUID1/UUID1.jsonl" "config-driven enablement works"
rm -rf "$ROOT"
echo ""

echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
