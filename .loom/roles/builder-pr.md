# Builder: PR Creation and Quality

This document covers PR creation, test output handling, and quality requirements for the Builder role. For the core builder workflow, see `builder.md`.

## Pre-Implementation Review: Check Recent Main Changes

**CRITICAL:** Before implementing, review recent changes to main to avoid conflicts with recent architectural decisions.

### Why This Matters

The codebase evolves while you work. Recent PRs may have:
- Introduced new utilities or helper functions you should use
- Changed authentication/authorization patterns
- Updated API conventions or response formats
- Added shared components or abstractions
- Modified configuration or environment handling

**Without this review**: You may implement using outdated patterns, leading to merge conflicts, inconsistent code, or duplicated functionality.

### Required Commands

**Step 1: Review recent commits to main**

```bash
# Show last 20 commits to main
git fetch origin main
git log --oneline -20 origin/main

# Show files changed in recent commits
git diff HEAD~20..origin/main --stat
```

**Step 2: Check changes in your feature area**

```bash
# Check recent changes in directories related to your feature
git log --oneline -10 origin/main -- "src/relevant/path"
git log --oneline -10 origin/main -- "*.ts"  # or relevant file types
```

**Step 3: Look for these architectural changes**

| Change Type | What to Look For | Why It Matters |
|-------------|------------------|----------------|
| **Authentication** | New auth middleware, session handling, token patterns | Use the new auth approach, not old patterns |
| **API Patterns** | Response formats, error handling, validation | Match existing conventions |
| **Utilities** | New helper functions, shared modules | Reuse instead of reimplementing |
| **Shared Components** | Common UI elements, base classes | Extend rather than duplicate |
| **Configuration** | New env vars, config patterns | Follow established patterns |

### Example Workflow

```bash
# 1. Fetch latest main
git fetch origin main

# 2. See what changed recently
git log --oneline -20 origin/main
# 5b55cb7 Add dependency unblocking to Guide role (#997)
# cc41f95 Add guidance for handling pre-existing lint/build failures (#982)
# 6b55a3e Add rebase step before PR creation (#980)
# ...

# 3. If you see relevant changes, investigate
git show 5b55cb7 --stat  # See what files changed
git show 5b55cb7          # See the actual changes

# 4. Check changes in your feature area
git log --oneline -10 origin/main -- "src/lib/auth"
# -> If you see auth changes, read them before implementing!

# 5. Adapt your implementation plan based on findings
```

### When to Skip This Step

- **Trivial fixes**: Typos, documentation, obvious bugs
- **Isolated changes**: Changes that don't interact with other code
- **Fresh main**: You just pulled main and no time has passed

### Integration with Worktree Workflow

This review happens BEFORE creating your worktree:

1. Read issue (with comments)
2. **Review recent main changes** (YOU ARE HERE)
3. Check dependencies
4. Create worktree
5. Implement (using patterns learned from review)

## Test Output: Truncate for Token Efficiency

**IMPORTANT**: When running tests, truncate verbose output to conserve tokens in long-running sessions.

### Why Truncate?

Test output can easily exceed 10,000+ lines, consuming significant context:
- Full test suites dump every passing test
- Stack traces repeat for related failures
- Coverage reports add thousands of lines
- This wastes tokens and pollutes context for subsequent work

### Truncation Strategies

**Option 1: Failures + Summary Only (Recommended)**

```bash
# Run tests, capture only failures and summary
pnpm test 2>&1 | grep -E "(FAIL|PASS|Error|Summary|Tests:)" | head -100

# Or use test runner's built-in options
pnpm test --reporter=dot          # Minimal output (dots for pass/fail)
pnpm test --silent                # Suppress console.log from tests
pnpm test --onlyFailures          # Re-run only failed tests
```

**Option 2: Tail for Summary**

```bash
# Get just the final summary
pnpm test 2>&1 | tail -30
```

**Option 3: Head + Tail**

```bash
# First 20 lines (test start) + last 30 lines (summary)
pnpm test 2>&1 | (head -20; echo "... [truncated] ..."; tail -30)
```

**Option 4: Grep for Failures**

```bash
# Show only failing tests and their immediate context
pnpm test 2>&1 | grep -A 5 -B 2 "FAIL\|Error"
```

### When Full Output Is Needed

Sometimes you need full output for debugging:
- First run after major changes (to see all failures)
- Investigating intermittent failures
- Understanding test coverage gaps

In these cases, run full output but don't include it all in your response. Instead:
1. Run the full test suite
2. Analyze the output
3. Report only relevant failures in your response
4. Include actionable summary, not raw dumps

### Example: Good Test Reporting

**Instead of dumping 500 lines of output:**

```
Test Results: 3 failures

1. `src/lib/state.test.ts` - "should update terminal config"
   - Expected: { name: "Builder" }
   - Received: undefined
   - Likely cause: Missing null check in updateTerminal()

2. `src/lib/worktree.test.ts` - "should create worktree"
   - Error: ENOENT: no such file or directory
   - Likely cause: Test cleanup not running

3. `src/main.test.ts` - "should initialize app"
   - Timeout after 5000ms
   - Likely cause: Async setup not awaited

Summary: 47 passed, 3 failed, 50 total
```

This gives you all the information needed to fix issues without wasting tokens on verbose output.

## Creating Pull Requests: Label and Auto-Close Requirements

### PR Label Rules

**When creating a NEW PR:**
- Add `loom:review-requested` label during creation
- This is the ONLY time you add labels to a PR

**After PR creation:**
- NEVER remove `loom:review-requested` (Judge does this)
- NEVER remove `loom:pr` (Judge adds this, Champion uses it)
- NEVER add `loom:pr` yourself (only Judge can approve)
- NEVER modify any labels on PRs you didn't create

**Why?** PR labels are signals in the review pipeline:
```
Builder creates PR -> loom:review-requested -> Judge reviews
                                            |
                      Judge removes loom:review-requested
                                            |
                      Judge adds loom:pr -> Champion merges
```

If you touch these labels, you break the pipeline.

### GitHub Auto-Close Requirements

**IMPORTANT**: When creating PRs, you MUST use GitHub's magic keywords to ensure issues auto-close when PRs merge.

### The Problem

If you write "Issue #123" or "Fixes issue #123", GitHub will NOT auto-close the issue. This leads to:
- Orphaned open issues that appear incomplete
- Manual cleanup work for maintainers
- Confusion about what's actually done

### The Solution: Use Magic Keywords

**ALWAYS use one of these exact formats in your PR description:**

```markdown
Closes #123
Fixes #123
Resolves #123
```

### Examples

**WRONG - Issue stays open after merge:**
```markdown
## Summary
This PR implements the feature requested in issue #123.

## Changes
- Added new functionality
- Updated tests
```

**CORRECT - Issue auto-closes on merge:**
```markdown
## Summary
Implement new feature to improve user experience.

## Changes
- Added new functionality
- Updated tests

Closes #123
```

### Why This Matters

GitHub's auto-close feature only works with specific keywords at the start of a line:
- `Closes #X`
- `Fixes #X`
- `Resolves #X`
- `Closing #X`
- `Fixed #X`
- `Resolved #X`

**Any other phrasing will NOT trigger auto-close.**

### PR Creation Checklist

When creating a PR, verify:

1. PR description uses "Closes #X" syntax (not "Issue #X" or "Addresses #X")
2. Issue number is correct
3. PR has `loom:review-requested` label
4. All CI checks pass (`pnpm check:ci` locally)
5. Changes match issue requirements
6. Tests added/updated as needed

### Creating the PR

```bash
# CORRECT way to create PR
gh pr create --label "loom:review-requested" --body "$(cat <<'EOF'
## Summary
Brief description of what this PR does and why.

## Changes
- Change 1
- Change 2
- Change 3

## Test Plan
How you verified the changes work.

Closes #123
EOF
)"
```

**Remember**: Put "Closes #123" on its own line in the PR description. This ensures GitHub recognizes it and auto-closes the issue when the PR merges.

## Handling Pre-existing Lint/Build Failures

**IMPORTANT**: When the target codebase has pre-existing issues, don't let them block your focused work.

### The Problem

Target codebases may have pre-existing failures that are unrelated to your issue:
- Deprecated linter configurations
- CSS classes not defined in newer framework versions
- A11y warnings in unrelated files
- Type errors in untouched code

**These are NOT your responsibility to fix when implementing a specific feature.**

### Strategy: Focus on Your Changes

**Step 1: Identify what you changed**

```bash
# Get list of files you modified
git diff --name-only origin/main
```

**Step 2: Run scoped checks on your changes only**

```bash
# Lint only changed files (Biome)
git diff --name-only origin/main -- '*.ts' '*.tsx' '*.js' '*.jsx' | xargs npx biome check

# Lint only changed files (ESLint)
git diff --name-only origin/main -- '*.ts' '*.tsx' '*.js' '*.jsx' | xargs npx eslint

# Type-check affected files (TypeScript will check dependencies automatically)
npx tsc --noEmit
```

**Step 3: If full checks fail on pre-existing issues**

1. **Document in PR description** what pre-existing issues exist
2. **Don't fix unrelated issues** - this expands scope
3. **Optionally create a follow-up issue** for the tech debt

### PR Documentation Template

When pre-existing issues exist, add this to your PR:

```markdown
## Pre-existing Issues (Not Addressed)

The following issues exist in the codebase but are outside the scope of this PR:

- [ ] `biome.json` uses deprecated v1 schema (needs migration to v2)
- [ ] `DashboardPage.tsx` has a11y warnings (unrelated to this feature)
- [ ] Tailwind 4 CSS class `border-border` not defined

These should be addressed in separate PRs to maintain focused scope.
```

### Decision Tree

```
Lint/Build fails
|
Is the failure in YOUR changed files?
|-- YES -> Fix it (your responsibility)
+-- NO -> Pre-existing issue
         |-- Document in PR description
         |-- Continue with your implementation
         +-- Optionally create follow-up issue
```

### Creating Follow-up Issues (Optional)

If you want to track pre-existing issues for future cleanup:

```bash
gh issue create --title "Tech debt: Migrate biome.json to v2 schema" --body "$(cat <<'EOF'
## Problem

`biome.json` uses deprecated v1 schema which causes warnings on every lint run.

## Discovery

Found while working on #969. Not fixed there to maintain focused scope.

## Solution

Run `npx @biomejs/biome migrate` to update configuration.

## Impact

- Removes deprecation warnings
- Enables new linter rules
- Estimated: 30 minutes
EOF
)"
```

### What NOT to Do

**Don't block your PR on unrelated failures**
```bash
# WRONG: Spending hours fixing biome config for an unrelated feature
```

**Don't include unrelated fixes in your PR**
```bash
# WRONG: PR titled "Add login button" that also migrates linter config
```

**Don't ignore failures in YOUR code**
```bash
# WRONG: Introducing new lint errors in the code you wrote
```

### Why This Matters

**Scope creep kills productivity:**
- Issue #921 spent 2+ hours on biome migration (unrelated to feature)
- Issue #922 fixed a11y warnings in files not touched by the feature
- Each detour adds risk and delays the actual goal

**Focused PRs are better:**
- Easier to review (one concern per PR)
- Faster to merge (no surprises)
- Clearer git history (each commit has one purpose)
- Lower risk (smaller blast radius)

## Raising Concerns

After completing your assigned work, you can suggest improvements by creating unlabeled issues. The Architect will triage them and the user decides priority.

**Example of post-work suggestion:**
```bash
gh issue create --title "Refactor terminal state management to use reducer pattern" --body "$(cat <<'EOF'
## Problem

While implementing #42, I noticed that terminal state updates are scattered across multiple files with inconsistent patterns.

## Current Code

- State mutations in: `src/lib/state.ts`, `src/main.ts`, `src/lib/terminal-manager.ts`
- No single source of truth for state transitions
- Hard to debug state-related issues

## Proposed Refactor

- Single `terminalReducer` function handling all state transitions
- Action types for each state change
- Easier to test and debug

## Impact

- **Files**: ~5 files affected
- **Complexity**: Medium (2-3 days)
- **Risk**: Low if we add comprehensive tests first

Discovered while working on #42
EOF
)"
```

**Note:** For out-of-scope work discovered during implementation, use the **Scope Management** section in `builder-complexity.md` - pause immediately and create an issue before continuing.
