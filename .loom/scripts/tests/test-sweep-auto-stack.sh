#!/bin/bash
# test-sweep-auto-stack.sh - Doc-shape tests for /sweep --auto-stack (issue #3759).
#
# /sweep is a markdown skill (prose-engineered), so these tests are
# documentation-shape checks: they verify the skill file documents the
# opt-in --auto-stack flag, the same-candidate-set body-text edge
# detection, the linear/single-parent + cycle guards, the confirmation
# gate reuse, the wave-ordering pass, and the per-issue DEPENDS_ON[N]
# generalization of the shipped --depends-on mechanics (#3729/#3747/#3752).
#
# The load-bearing guard here is the "same-candidate-set only" restriction:
# a future edit must not silently broaden detection to arbitrary external
# issues. That is asserted explicitly below.
#
# Run from anywhere — uses an absolute path to the skill file via the
# script's own directory.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWEEP_MD="$SCRIPT_DIR/../../../defaults/.claude/commands/loom/sweep.md"
GUIDE_MD="$SCRIPT_DIR/../../../defaults/roles/guide.md"

if [[ ! -f "$SWEEP_MD" ]]; then
    echo "FAIL: skill file not found at $SWEEP_MD" >&2
    exit 1
fi

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

echo "--- Flag documentation + validation ---"

assert_contains "--auto-stack flag documented in Optional flags" '**`--auto-stack`**'
assert_contains "--auto-stack recorded as AUTO_STACK flag in Validation rules" 'AUTO_STACK=true|false'
assert_contains "--auto-stack is a bare flag (no value)" 'a bare flag (consumes no value)'
assert_contains "--auto-stack default off" 'default `AUTO_STACK=false`'
assert_contains "--auto-stack no-op in Mode C" 'in Mode C it is silently ignored'
assert_contains "Absent-flag byte-for-byte-unchanged contract" 'Absent the flag, behavior is byte-for-byte unchanged'

echo
echo "--- Detection: authoritative body-text signal, same-candidate-set only ---"

assert_contains "Detection reuses guide.md parse_dependencies convention" 'parse_dependencies'
assert_contains "Detection restricted to Depends on / Requires" '(Depends on|Requires) #[0-9]+'
assert_contains "Blocked by deliberately excluded from stacking detection" 'EXCLUDES `Blocked by`'
assert_contains "body field added to existing gh issue view read (no new API call)" 'no new API call'
# THE load-bearing guard: same-candidate-set restriction must be stated explicitly.
assert_contains "Edge only when parent is in this candidate set" 'only when `#A` is also a member of this sweep invocation'"'"'s own deduplicated candidate list'
assert_contains "Out-of-set Depends on left untouched (loom:blocked path)" 'is left completely untouched'
assert_contains "Same-candidate-set restriction called load-bearing" 'This "same-candidate-set only" restriction is load-bearing'

echo
echo "--- Linear single-parent + cycle guards ---"

assert_contains "At most one in-set parent (mirrors Option<u32>)" 'at most **one** in-set parent'
assert_contains "First-match-wins on multiple parents" 'take the **first**'
assert_contains "Do not build a Vec of parents" 'do **not** build a `Vec`'
assert_contains "Multi-parent warning logged" 'honoring #a only (single-parent edges)'
assert_contains "Cyclic edges dropped, not silently oriented" 'drop every edge in the cycle'
assert_contains "Cycle warning logged" 'dropped cyclic stacking edges'
assert_contains "No diamond support" 'Diamonds / multi-parent stacks are structurally unrepresentable'

echo
echo "--- Confirmation gate reuse ---"

assert_contains "Detected stacking pairs block present" 'Detected stacking pairs'
assert_contains "Mode A prompts only when an edge was found" 'gains a confirmation prompt **only when `--auto-stack` actually found ≥1 edge**'
assert_contains "Zero-edge Mode A stays prompt-free" 'zero-edge `--auto-stack` run on Mode A stays prompt-free'
assert_contains "Declining exits cleanly" 'Declining exits cleanly'

echo
echo "--- Wave ordering (parent at-or-before child) ---"

assert_contains "Parent lands at or before child's wave" 'every parent lands in a wave at or before its child'"'"'s wave'
assert_contains "Parent/child may share a wave" 'may** land in the *same* wave'
assert_contains "Same-wave child branches off parent branch not main snapshot" 'not** off the shared pre-wave `main` snapshot'
assert_contains "Topological pass restricted to linear chains" 'no general DAG solver'

echo
echo "--- Per-issue DEPENDS_ON[N] generalization (mechanics unchanged) ---"

assert_contains "Per-issue DEPENDS_ON[N] map" 'DEPENDS_ON[N]'
assert_contains "Generalizes single global DEPENDS_ON value" 'the pre-existing single global `DEPENDS_ON` value'
assert_contains "worktree.sh --base mechanics untouched" 'worktree.sh N --base feature/issue-<parent>'
assert_contains "gh pr create --base mechanics untouched" 'gh pr create --base feature/issue-<parent>'
assert_contains "Explicit --depends-on never overridden by detected edge" 'never override'
assert_contains "Daemon path forwards depends_on per candidate" 'mcp__loom__dispatch_sweep(kind={"Issue": N}, depends_on=<parent>)'
assert_contains "No daemon-side code change" 'no daemon-side code change'

echo
echo "--- Explicitly out of scope for v1 ---"

assert_contains "File-overlap heuristic deferred" 'file-overlap-heuristic'
assert_contains "Reactive #3647 overlap gate stays the backstop" '#3647'
assert_contains "Cross-sweep coordination is #3768's scope" '#3768'
assert_contains "No change to merge-pr.sh / reconcile-stack.sh / worktree.sh" 'any change to `merge-pr.sh` / `reconcile-stack.sh` / `worktree.sh`'

echo
echo "--- Anti-regressions (must NOT appear) ---"

# Must not repurpose Blocked by into stacking (belongs to loom:blocked machinery).
assert_not_contains "Blocked-by NOT used as a stacking edge phrase" '(Blocked by|Depends on|Requires) #[0-9]+'
# Must not invent a new label.
assert_not_contains "No new loom:stacked label invented" 'loom:stacked'

echo
echo "--- Cross-file: guide.md regex convention still present (reused, not modified) ---"

if [[ -f "$GUIDE_MD" ]]; then
    if grep -qF -- '(Blocked by|Depends on|Requires|\- \[.\]) #[0-9]+' "$GUIDE_MD"; then
        echo "PASS: guide.md parse_dependencies regex convention intact (reused by --auto-stack)"
        PASS=$((PASS + 1))
    else
        echo "FAIL: guide.md parse_dependencies regex convention missing — --auto-stack detection derives from it" >&2
        FAIL=$((FAIL + 1))
    fi
else
    echo "PASS: guide.md not present in this tree (skipping cross-file check)"
    PASS=$((PASS + 1))
fi

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
