#!/usr/bin/env bash
# test-curator-rightsizing.sh - Prompt-lint for issue #9026 (backlog rightsizing).
#
# #9026 embedded three rules in the Curator role prompt so Curator stops
# fragmenting work into micro-issues, each of which pays the full
# Curator -> Builder -> CI -> Judge pipeline tax:
#   1. sibling batching (sibling tool bugs / multi-file doc passes / related
#      test fixes filed as ONE issue),
#   2. a ~20-45 min / 2-8 file sizing target, stated as a heuristic and
#      explicitly reconciled with the existing decomposition thresholds
#      (>6 h / >8 files / >400 LOC, builder-complexity.md's "under 4 h"),
#   3. a consolidation gate before `loom:curated` that never absorbs a
#      claimed/building/promoted/epic issue, a child of a parent, or one not
#      verified PR-free (check-open-pr exit 1 only), and preserves the
#      absorbed issues' original content and links.
#
# These rules live in prose that a later trim (the markdown token ratchet
# pushes every prompt edit to remove as much as it adds) could silently drop,
# so this suite asserts each load-bearing clause is still present, that the
# Decision Tree and Quality Checklist still route to the gate, that guide.md
# still hands sibling micro-issues to Curator, and that the generated
# agent-skill copies carry the same text.
#
# Hermetic: reads files only — no forge, network, or loom-daemon binary.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Assert against the defaults/ SOURCE (what this repo ships and what the
# generated skills are built from), not an installed .claude/ mirror that may
# lag a resync.
CURATOR_MD="$REPO_ROOT/defaults/.claude/commands/loom/curator.md"
GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
CURATOR_SKILL="$REPO_ROOT/defaults/.agents/skills/loom-curator/SKILL.md"
GUIDE_SKILL="$REPO_ROOT/defaults/.agents/skills/loom-guide/SKILL.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

for f in "$CURATOR_MD" "$GUIDE_MD" "$CURATOR_SKILL" "$GUIDE_SKILL"; do
    if [[ ! -f "$f" ]]; then
        echo -e "${RED}FAIL${NC}: missing $f" >&2
        exit 1
    fi
done

# section <file> <heading-regex> — print one `## ` section (heading to the
# next `## ` heading), so an assertion cannot be satisfied by a stray match
# elsewhere in a 2000-line prompt.
section() {
    awk -v h="$2" '
        $0 ~ "^## " { if (f) exit; if ($0 ~ h) f = 1 }
        f { print }
    ' "$1"
}

# expect <label> <haystack> <fixed-string>
expect() {
    if grep -qF -- "$3" <<< "$2"; then pass "$1"; else fail "$1 (missing: $3)"; fi
}

echo "Test group 1: curator.md carries the Backlog Rightsizing section"
RS="$(section "$CURATOR_MD" '^## Backlog Rightsizing')"
if [[ -n "$RS" ]]; then pass "section '## Backlog Rightsizing' exists"; else fail "section '## Backlog Rightsizing' exists"; fi
expect "names the pipeline tax it amortizes"          "$RS" "Curator → Builder → CI → Judge"
expect "is framed as a heuristic, not a gate"         "$RS" "Heuristics, not a gate"
expect "sizing target: 20-45 min"                     "$RS" "~20–45 min of Builder work"
expect "sizing target: 2-8 related files"             "$RS" "2–8 related files"
expect "batching: sibling tool bugs"                  "$RS" "sibling tool bugs"
expect "batching: multi-file doc pass"                "$RS" "multi-file doc pass"
expect "batching: related test fixes"                 "$RS" "related test fixes"
expect "reconciled with curator decomposition thresholds" "$RS" ">6 h, >8 files, >400 LOC"
expect "reconciled with builder-complexity.md"        "$RS" "builder-complexity.md"
expect "floor-not-ceiling framing"                    "$RS" "Floor, not ceiling"

echo ""
echo "Test group 2: the consolidation gate's guardrails"
expect "gate runs before loom:curated"                "$RS" "Consolidation gate — before \`loom:curated\`"
expect "only triage/unlabeled siblings"               "$RS" "\`loom:triage\`/unlabeled siblings"
for label in 'loom:curating' 'loom:building' 'loom:issue' 'loom:blocked' 'loom:operator-only'; do
    expect "never absorbs a $label issue"             "$RS" "$label"
done
expect "never absorbs a hard-excluded issue"          "$RS" "hard-exclusion label"
for label in 'loom:epic' 'loom:epic-phase'; do
    expect "never absorbs a $label issue"             "$RS" "/\`$label\`"
done
expect "never absorbs a child of a parent issue"      "$RS" "a parent (sub-issue, \`Part of #N\`, \`[Parent #N]\`, or in a parent's task list)"
# check-open-pr's contract (loom-daemon/src/forge_check_open_pr.rs): ONLY
# exit 1 is a verified absence; 0 = open PR, 3 = forge declined, 5 = probe
# failed. The gate closes issues, so it must fold only on exit 1 and fail
# closed on everything else — pin the clause, not just the command name.
expect "open-PR probe: fold only on exit 1"           "$RS" "(\`loom-daemon forge check-open-pr <N>\`): fold only on exit 1;"
expect "open-PR probe: every other exit fails closed" "$RS" "any other exit (0 = open PR, 3, 5, …) ⇒ skip (fail closed)"
if grep -qE 'exit 0 ⇒ skip' <<< "$RS"; then
    fail "open-PR probe: no fail-open 'exit 0 ⇒ skip' wording"
else
    pass "open-PR probe: no fail-open 'exit 0 ⇒ skip' wording"
fi
expect "preserves original content verbatim"          "$RS" "quoted verbatim"
expect "preserves links (Consolidated from)"          "$RS" "## Consolidated from"
expect "cross-links the closed sibling"               "$RS" "Consolidated into #<survivor>"
expect "closes with the duplicate convention"         "$RS" "gh issue close <N> --reason \"not planned\""
expect "ambiguity falls back to a cross-link"         "$RS" "Related: #N"

echo ""
echo "Test group 3: the gate is reachable from the curation flow"
TRIAGE="$(section "$CURATOR_MD" '^## Triage: Ready or Needs Enhancement')"
expect "Decision Tree routes to the consolidation gate" "$TRIAGE" "run the consolidation gate (\"Backlog Rightsizing\" below)"
if grep -qF 'Mark it `loom:curated` immediately' <<< "$TRIAGE"; then
    fail "Decision Tree does not say 'mark immediately' ahead of the gate"
else
    pass "Decision Tree does not say 'mark immediately' ahead of the gate"
fi
CHECK="$(section "$CURATOR_MD" '^## Issue Quality Checklist')"
expect "Quality Checklist has a Right-sized item"     "$CHECK" "**Right-sized**"
DECOMP="$(section "$CURATOR_MD" '^## Decomposing Oversized Issues')"
expect "decomposition sizes children per rightsizing" "$DECOMP" "size each child per \"Backlog Rightsizing\""
TOC="$(awk '/<!-- toc:begin/,/<!-- toc:end -->/' "$CURATOR_MD")"
expect "TOC lists the new section"                    "$TOC" "[Backlog Rightsizing (#9026)]"

echo ""
echo "Test group 4: guide.md hands sibling micro-issues to Curator"
PRIO="$(section "$GUIDE_MD" '^## Tier Labels and Duplicate Checks')"
expect "guide defers sibling micro-issues to Curator" "$PRIO" "sibling micro-issues: leave for Curator"
expect "guide points at the curator section"          "$PRIO" "\"Backlog Rightsizing\""

echo ""
echo "Test group 5: generated agent-skill copies carry the same rules"
# The skills are generated by `loom-daemon generate-agent-skills`; CI's
# --check proves byte-sync. This group only proves the regenerate step was
# not skipped in a way that leaves the rules out of the cross-vendor surface.
SRS="$(section "$CURATOR_SKILL" '^## Backlog Rightsizing')"
expect "loom-curator skill has the section"           "$SRS" "Consolidation gate — before \`loom:curated\`"
expect "loom-guide skill has the handoff"             "$(cat "$GUIDE_SKILL")" "sibling micro-issues: leave for Curator"

echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
