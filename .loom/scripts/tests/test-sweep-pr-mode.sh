#!/bin/bash
# test-sweep-pr-mode.sh - Smoke tests for /sweep PR-set mode (Mode C, #3384).
#
# /sweep is a markdown skill (prose-engineered), so these tests are
# documentation-shape checks: they verify the skill file contains the
# required Mode C structure, the Mode A/B/C classifier rules, the
# PR-set dry-run output format, and the load-bearing constraints from
# #3289 / #3298 / #3373.
#
# Run from anywhere — uses an absolute path to the skill file via the
# script's own directory.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWEEP_SKILL_DIR="$SCRIPT_DIR/../../../defaults/.claude/commands/loom"

if [[ ! -d "$SWEEP_SKILL_DIR" ]]; then
    echo "FAIL: skill directory not found at $SWEEP_SKILL_DIR" >&2
    exit 1
fi

# The /loom:sweep skill in document order (#7726 split the monolithic
# sweep.md into a dispatcher + 11 sibling reference files). Concatenate them
# into one file so every assertion below finds its content wherever it now
# lives — mirrors SWEEP_SKILL_FILES in
# loom-daemon/tests/sweep_md_doc_lint.rs.
SWEEP_SKILL_FILES=(
    sweep.md
    sweep-arguments.md
    sweep-examples.md
    sweep-execution-model.md
    sweep-backend-detection.md
    sweep-scheduling-signals.md
    sweep-dry-run.md
    sweep-mode-c-lifecycle.md
    sweep-wave-lifecycle.md
    sweep-summary-output.md
    sweep-run-hygiene.md
    sweep-reference.md
)
SWEEP_MD="$(mktemp)"
trap 'rm -f "$SWEEP_MD"' EXIT
for f in "${SWEEP_SKILL_FILES[@]}"; do
    path="$SWEEP_SKILL_DIR/$f"
    if [[ ! -f "$path" ]]; then
        echo "FAIL: skill sibling file not found at $path" >&2
        exit 1
    fi
    cat "$path" >> "$SWEEP_MD"
done

PASS=0
FAIL=0

# Check that a substring appears in the skill file.
# IMPORTANT: callers must pass needles via single-quoted strings to avoid
# the shell expanding backticks (`...`) as command substitution.
assert_contains() {
    local desc="$1" needle="$2"
    if grep -qF -- "$needle" "$SWEEP_MD"; then
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (missing literal: $needle)" >&2
        FAIL=$((FAIL + 1))
    fi
}

# Check that a regex pattern appears in the skill file.
assert_matches() {
    local desc="$1" pattern="$2"
    if grep -qE -- "$pattern" "$SWEEP_MD"; then
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (no match for pattern: '$pattern')" >&2
        FAIL=$((FAIL + 1))
    fi
}

# Check that a substring does NOT appear in the skill file.
assert_not_contains() {
    local desc="$1" needle="$2"
    if grep -qF -- "$needle" "$SWEEP_MD"; then
        echo "FAIL: $desc (forbidden literal present: '$needle')" >&2
        FAIL=$((FAIL + 1))
    else
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    fi
}

echo "--- Mode A/B/C classifier rules (regression + new) ---"

# Mode A (regression guard from #3318 — must still work).
assert_matches "Mode A heading present" '^### Mode A — Explicit numeric list'
assert_contains "Mode A regression: caret-hash-digit regex preserved" 'regex `^#?'
assert_contains "Mode A regression: bit-for-bit compatibility note" "must remain bit-for-bit compatible"

# Mode B (regression guard from #3318).
assert_matches "Mode B heading present" '^### Mode B — Natural-language interpretation'
assert_contains "Mode B regression: gh issue list translation" 'gh issue list'

# Mode C (new in #3384).
assert_matches "Mode C heading present" '^### Mode C — PR-set mode'
assert_contains "Mode C: --prs flag documented" '--prs'
assert_contains "Mode C: gh pr list translation (not gh issue list)" 'gh pr list'
assert_contains "Mode C: NL trigger phrases listed" 'pull requests'
assert_contains "Mode C: review-requested NL trigger" 'review-requested PRs'
assert_contains "Mode C: merge-ready NL trigger" 'merge-ready PRs'

echo
echo "--- Mode-selection precedence ---"

assert_contains "Mode-selection precedence rules present" 'Mode-selection precedence'
assert_contains "Mode-selection: --prs flag is strongest signal" 'If `--prs` is present'
assert_contains "Mode-selection: NL trigger as second priority" 'PR-side NL trigger'

echo
echo "--- Per-PR routing by current label (Mode C) ---"

assert_contains "Routing: review-requested → Judge" 'C1a'
assert_contains "Routing: changes-requested → Doctor → Judge" 'C1b'
assert_contains "Routing: loom:pr → Merge" 'C1c'
assert_contains "Routing: Judge-phase dispatch for review-requested" 'loom:review-requested'
assert_contains "Routing: Doctor-phase dispatch for changes-requested" 'loom:changes-requested'
assert_contains "Routing: Merge for loom:pr" 'merge-pr.sh P --auto'

echo
echo "--- Skip rules (Mode C) ---"

assert_contains "Skip: state != OPEN" 'state != OPEN'
assert_contains "Skip: loom:blocked PRs" 'loom:blocked'
assert_contains "Skip: no actionable label" 'no actionable label'
assert_contains "Skip: multiple actionable labels" 'multiple actionable labels'
assert_contains "Skip: loom:operator-only PRs" 'loom:operator-only'

echo
echo "--- Load-bearing constraints (#3289, #3298, #3373) ---"

# #3289: one level deep, never spawn a nested /loom:sweep as subagent.
assert_contains "One-level-deep callout preserved" 'One level deep'
assert_contains "Never-dispatch-nested-orchestrator-as-subagent guard preserved (issue-side)" 'Do NOT, under any circumstances, dispatch a nested orchestrator skill (`/loom:sweep`) as a subagent from `/loom:sweep`'
assert_contains "Never-invoke /loom:sweep, /loom:judge, or /loom:doctor as a subagent (Mode C constraints)" 'Never invoke `/loom:sweep`, `/loom:judge`, or `/loom:doctor` as a subagent from `/loom:sweep`'

# Sequential per-PR Judge.
assert_contains "Per-PR Judge sequential within wave" 'Per-PR Judge is sequential'

# Configurable Doctor→Judge cycle cap (#3668).
assert_contains "Configurable Doctor→Judge cycle cap (Mode C, C1b)" 'up to `sweep.max_doctor_cycles`'
assert_contains "Cap-reached block note (Mode C, C1b)" 'cap reached'
assert_contains "Distinct-defect exception referenced (Mode C, C1b)" 'distinct defect'

# #3373: checkpoint reuse via closingIssuesReferences.
assert_contains "Checkpoint scope via closingIssuesReferences" 'closingIssuesReferences'
assert_contains "Checkpoint fallback for PRs without Closes #N" "lacks a Closes #N reference"

echo
echo "--- Dry-run output (PR-set format) ---"

assert_contains "PR-set dry-run header" '/loom:sweep --prs --dry-run plan'
assert_contains "PR-set dry-run shows would-Judge" 'would Judge'
assert_contains "PR-set dry-run shows would-Doctor-then-Judge" 'would Doctor → Judge'
assert_contains "PR-set dry-run shows would-merge" 'would merge'
assert_contains "PR-set dry-run shows would-skip with reason" 'would skip (no actionable label)'
assert_contains "PR-set dry-run footer total" 'No PRs were modified'

# Dry-run gate inviolable contract preserved (issue-side regression).
assert_contains "Dry-run gate: no label edits" 'no label edits'
assert_contains "Dry-run gate: no merge-pr.sh" 'no `merge-pr.sh`'
assert_contains "Dry-run gate: no Task/subagent dispatch" 'no Task/subagent dispatch'

echo
echo "--- Wave model (Mode C: size-1, --builders-per-wave ignored) ---"

assert_contains "Mode C waves are size-1" 'size-1 wave'
assert_contains "--builders-per-wave ignored in Mode C" '`--builders-per-wave N` is silently ignored in Mode C'

echo
echo "--- Examples section coverage ---"

assert_contains "Example: explicit numeric PR list with --prs" '/loom:sweep --prs 100 101 102'
assert_contains "Example: NL description with --prs" '/loom:sweep --prs all open loom:pr'
assert_contains "Example: NL trigger without --prs" '/loom:sweep all open loom:pr PRs'
assert_contains "Example: PR-set dry-run" '/loom:sweep --prs 100 101 102 --dry-run'

echo
echo "--- Constraints + Limitations updated ---"

assert_contains "Constraints: Mode C skips Curator/Approval/Builder" 'Mode C skips Curator'
assert_contains "Constraints: uniform no-gh-pr-merge mandate" 'uniform across Modes A/B/C'
assert_contains "Limitations entry: Mode C (#3384)" '#3384'
assert_contains "Limitations entry: mixed-mode won't fix" 'Mixed-mode invocations'
assert_contains "Limitations entry: PRs without Closes #N" 'PRs without `Closes #N` references'

echo
echo "--- Anti-regressions (must NOT appear) ---"

# Must not invent a separate /sweep-pr verb.
assert_not_contains "No /sweep-pr verb invented" '/sweep-pr'
# Must not create new labels.
assert_not_contains "No new label 'loom:judging' invented" 'loom:judging'
assert_not_contains "No new label 'loom:doctoring' invented" 'loom:doctoring'
# Must not call gh pr merge directly.
assert_not_contains "No direct gh pr merge call" 'gh pr merge --squash'

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
