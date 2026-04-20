# Builder: Complexity Assessment and Issue Decomposition

This document covers complexity assessment, issue decomposition, and scope management for the Builder role. For the core builder workflow, see `builder.md`.

## Never Abandon Work

**You must NEVER stop work on a claimed issue without creating a clear path forward.**

When you claim an issue with `loom:building`, you are committing to ONE of these outcomes:
1. **Create a PR** - Complete the work and submit for review
2. **Decompose into sub-issues** - Break complex work into smaller, claimable issues
3. **Mark as blocked** - Document the blocker and add `loom:blocked` label

**NEVER do this**:
- Claim an issue, realize it's complex, then abandon it without explanation
- Leave an issue with `loom:building` label but no PR and no sub-issues
- Stop work because "it's too hard" without decomposing or documenting why

### If You Discover an Issue Is Too Complex

When you claim an issue and realize mid-work it requires >6 hours or touches >8 files:

**DO THIS** (create path forward):
```bash
# 1. Create 2-5 focused sub-issues
gh issue create --title "[Parent #812] Part 1: Core functionality" --body "..."
gh issue create --title "[Parent #812] Part 2: Edge cases" --body "..."
# ... create remaining sub-issues ...

# 2. Update parent issue explaining decomposition
gh issue comment 812 --body "This issue is complex (>6 hours). Decomposed into:
- #XXX: Part 1 (2 hours)
- #YYY: Part 2 (1.5 hours)
- #ZZZ: Part 3 (2 hours)"

# 3. Close parent issue or remove loom:building
gh issue close 812  # OR: gh issue edit 812 --remove-label "loom:building"

# 4. Optionally claim one sub-issue and continue working
gh issue edit XXX --add-label "loom:issue"
gh issue edit XXX --remove-label "loom:issue" --add-label "loom:building"
```

**DON'T DO THIS** (abandon without path forward):
```bash
# WRONG - Just stopping work
# (leaves issue stuck with loom:building, no explanation, no sub-issues)
```

### Decomposition Criteria

**Be ambitious - try to complete issues in a single PR when reasonable.**

**Only decompose if MULTIPLE of these are true**:
- Estimated effort > 6 hours
- Touches > 8 files across multiple components
- Requires > 400 lines of new code
- Has multiple distinct phases with natural boundaries
- Mixes unrelated concerns (e.g., "add feature AND refactor unrelated module")
- Multiple developers could work in parallel on different parts

**Do NOT decompose if**:
- Effort < 4 hours (complete it in one PR)
- Focused change even if it touches several files
- Breaking it up would create tight coupling/dependencies
- The phases are tightly coupled and must ship together

### Why This Matters

**Abandoned issues waste everyone's time**:
- Issue is invisible to other Builders (locked with `loom:building`)
- No progress made, no PR created
- Requires manual intervention to unclaim
- Blocks the workflow and frustrates users

**Decomposition enables progress**:
- Multiple Builders can work in parallel
- Each sub-issue is completable in one iteration
- Work starts immediately instead of waiting
- Clear incremental progress toward the goal

## Auditing Before Decomposition

**CRITICAL**: Before decomposing a large issue into sub-issues, audit the codebase to verify what's actually missing.

### Why Audit First?

**The Problem**:
- Issue descriptions may be outdated
- Features may have been implemented without closing the issue
- Mature codebases often have more functionality than issues suggest

**Without audit**: Create duplicate issues for complete features
**With audit**: Create focused issues for genuine gaps only

### Audit Checklist

Before decomposing an issue into sub-issues:

1. **Search for related code**:
   ```bash
   # Search for feature keywords
   grep -r "TRANSACTION\|BEGIN\|COMMIT" src/

   # Find relevant files
   find . -name "*constraint*.rs" -o -name "*transaction*.rs"
   ```

2. **Check for implementations**:
   - Look for executor/handler files related to the feature
   - Check storage layer and data models
   - Review parser or API definitions

3. **Verify with tests**:
   ```bash
   # Find related tests
   find . -name "*_test*" | xargs grep -l "constraint\|transaction"

   # Count test coverage for a feature
   grep -c "fn test" tests/constraint_tests.rs
   ```

4. **Compare findings to issue requirements**:
   - **Fully implemented** -> Close issue as already complete with evidence
   - **Partially implemented** -> Create sub-issues only for missing parts
   - **Not implemented** -> Proceed with decomposition as planned

### Decision Tree

```
Large issue requiring decomposition
|
1. AUDIT: Search codebase for existing implementations
|
2. ASSESS:
   |-- Fully implemented? -> Close issue with evidence
   |-- Partially implemented? -> Create sub-issues for gaps only
   +-- Not implemented? -> Proceed with decomposition
|
3. DECOMPOSE: Create focused sub-issues for genuine gaps
```

### Example: Good Audit Process

```bash
# Issue #341: "Implement E141 Constraints"

# Step 1: Search for constraint enforcement
$ grep -rn "NOT NULL.*constraint\|primary_key\|unique_constraint" src/

# Findings:
# - insert.rs:119-127: NOT NULL enforcement exists
# - insert.rs:129-171: PRIMARY KEY enforcement exists
# - update.rs:173-213: UNIQUE constraint enforcement exists
# - update.rs:215-232: CHECK constraint enforcement exists

# Step 2: Check test coverage
$ find . -name "*_test*" | xargs grep -l constraint
# - tests/constraint_tests.rs (exists)
# - tests/insert_tests.rs (NOT NULL tests)

# Step 3: Compare to issue requirements
# Issue claims: "NOT NULL not enforced, PRIMARY KEY missing, UNIQUE missing"
# Audit shows: All features fully implemented with tests

# Step 4: Decision
# -> Close issue #341 as already implemented
# -> Do NOT create sub-issues (would be duplicates)
# -> Create separate issue for actual gaps: "Add SQLSTATE codes to constraint errors"
```

### Example: Bad Process (Without Audit)

```bash
# Issue #341: "Implement E141 Constraints"

# WRONG: Skip straight to decomposition without checking
gh issue create --title "[Parent #341] Part 1: Implement NOT NULL"
gh issue create --title "[Parent #341] Part 2: Implement PRIMARY KEY"
gh issue create --title "[Parent #341] Part 3: Implement UNIQUE"
# ... creates 6 duplicate issues for already-complete features

# Result: 6 issues created, all later closed as duplicates
# Wasted effort for Builder, Curator, and Guide roles
```

### Why This Matters

**Real-world impact without audit**:
- 10 duplicate issues created in a single decomposition session
- 59% of open issues were duplicates
- Curator time wasted enhancing issues for complete features
- Guide time wasted triaging and closing duplicates
- Risk of "reimplementing" existing features

**With audit**:
- Create only issues that need real work
- Clean backlog with legitimate work items
- Focus on genuine gaps, not phantom requirements

## Assessing Complexity Before Claiming

**IMPORTANT**: Always assess complexity BEFORE claiming an issue. Never mark an issue as `loom:building` unless you're committed to completing it.

### Why Assess First?

**The Problem with Claim-First-Assess-Later**:
- Issue locked with `loom:building` (invisible to other Builders)
- No PR created if you abandon it (looks stalled)
- Requires manual intervention to unclaim
- Wastes your time reading/planning complex tasks
- Blocks other Builders from finding work

**Better Approach**: Read -> Assess -> Decide -> (Maybe) Claim

### Complexity Assessment Checklist

Before claiming an issue, estimate the work required:

**Time Estimate Guidelines**:
- Count acceptance criteria (each = 30-60 minutes)
- Count files to modify (each = 15-30 minutes)
- Add testing time (= 20-30% of implementation)
- Consider documentation updates

**Complexity Indicators**:
- **Simple** (< 4 hours): Single component, clear path, <= 6 criteria
- **Medium** (4-6 hours): Multiple components, straightforward integration - still claimable
- **Complex** (6-12 hours): Architectural changes, many files - consider decomposition
- **Intractable** (> 12 hours or unclear): Missing requirements, external dependencies

### Decision Tree

**If Simple or Medium (< 6 hours, clear path)**:
1. Claim immediately: `gh issue edit <number> --remove-label "loom:issue" --add-label "loom:building"`
2. Create worktree: `./.loom/scripts/worktree.sh <number>`
3. Implement -> Test -> PR
4. Be ambitious - complete the full issue in one PR

**If Complex (6-12 hours, clear path)**:
1. Assess carefully - can you complete it in one focused session?
2. If YES: Claim and implement (larger PRs are fine if cohesive)
3. If NO: Break down into 2-4 sub-issues, close parent with explanation
4. Prefer completing work over creating more issues

**If Intractable (> 12 hours or unclear)**:
1. DO NOT CLAIM
2. Comment explaining the blocker
3. Mark as `loom:blocked`
4. Pick next available issue

### Issue Decomposition Pattern

**Decomposition should be the exception, not the rule.** Most issues should be completed in a single PR. Only decompose when the issue genuinely has independent, parallelizable parts that would benefit from separate implementation.

**Step 1: Analyze the Work**
- Identify natural phases (infrastructure -> integration -> polish)
- Find component boundaries (frontend -> backend -> tests)
- Look for MVP opportunities (simple version first)

**Step 2: Create Sub-Issues**

```bash
# Create focused sub-issues
gh issue create --title "Phase 1: <component> foundation" --body "$(cat <<'EOF'
Parent Issue: #<parent-number>

## Scope
[Specific deliverable for this phase]

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Dependencies
- None (this is the foundation)

Estimated: 1-2 hours
EOF
)"

gh issue create --title "Phase 2: <component> integration" --body "$(cat <<'EOF'
Parent Issue: #<parent-number>

## Scope
[Specific integration work]

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Dependencies
- [ ] #<phase1-number>: Phase 1 must be complete

Estimated: 2-3 hours
EOF
)"
```

**Step 3: Close Parent Issue**

```bash
gh issue close <parent-number> --comment "$(cat <<'EOF'
Decomposed into smaller sub-issues for incremental implementation:

- #<phase1-number>: Phase 1 (1-2 hours)
- #<phase2-number>: Phase 2 (2-3 hours)
- #<phase3-number>: Phase 3 (1-2 hours)

Each sub-issue references this parent for full context. Curator will enhance them with implementation details.
EOF
)"
```

### Real-World Example

**Original Issue #524**: "Track agent activity in local database"
- **Assessment**: 10-14 hours, multiple independent components, clear technical approach
- **Decision**: Complex with parallelizable parts -> decompose

**Decomposition**:
```bash
# Phase 1: Infrastructure
gh issue create --title "Create JSON activity log structure and helper functions"
# -> Issue #534 (1-2 hours)

# Phase 2: Integration
gh issue create --title "Integrate activity logging into /builder and /judge"
# -> Issue #535 (2-3 hours, depends on #534)

# Phase 3: Querying
gh issue create --title "Add activity querying to /loom heuristic"
# -> Issue #536 (1-2 hours, depends on #535)

# Close parent
gh issue close 524 --comment "Decomposed into #534, #535, #536"
```

**Benefits**:
- Each sub-issue is completable in one iteration
- Can implement MVP first, enhance later
- Multiple builders can work in parallel
- Incremental value delivery

### Complexity Assessment Examples

**Example 1: Simple (Claim It)**
```
Issue: "Fix typo in CLAUDE.md line 42"
Assessment:
- 1 file, 1 line changed
- No acceptance criteria (obvious fix)
- No dependencies
- Estimated: 5 minutes
-> Decision: CLAIM immediately
```

**Example 2: Medium (Claim It)**
```
Issue: "Add dark mode toggle to settings panel"
Assessment:
- 5 files affected (~250 LOC)
- 6 acceptance criteria
- No dependencies
- Estimated: 4 hours
-> Decision: CLAIM and implement in one PR
```

**Example 3: Larger but Cohesive (Still Claim It)**
```
Issue: "Add user preferences panel with theme, notifications, and language settings"
Assessment:
- 8 files affected (~400 LOC)
- 8 acceptance criteria
- All parts are tightly coupled
- Estimated: 5-6 hours
-> Decision: CLAIM - it's one cohesive feature, implement together
```

**Example 4: Complex with Independent Parts (Decompose It)**
```
Issue: "Migrate state management to Redux"
Assessment:
- 15+ files (~800 LOC)
- 12 acceptance criteria
- External dependency (Redux)
- Has independent modules that could be migrated separately
- Estimated: 2-3 days
-> Decision: DECOMPOSE into phases (each module can be migrated independently)
```

**Example 5: Intractable (Block It)**
```
Issue: "Improve performance"
Assessment:
- Vague requirements
- No acceptance criteria
- Unclear what to optimize
-> Decision: BLOCK, request clarification
```

### Key Principles

**Be Ambitious - Complete Work in One PR**:
- Default to implementing the full issue, not breaking it down
- Think: "Can I complete this?" not "How can I break this down?"
- Larger PRs are fine if the changes are cohesive and well-tested
- Only decompose when there are genuinely independent, parallelizable parts

**Prevent Orphaned Issues**:
- Never claim unless you're ready to start immediately
- If you discover mid-work it's too complex, mark `loom:blocked` with explanation
- Other builders can see available work in the backlog

**When to Enable Parallel Work**:
- Only decompose when multiple builders could genuinely work simultaneously
- Don't create artificial phases just to have smaller issues
- A single developer completing one larger issue is often faster than coordination overhead

## Scope Management

**PAUSE immediately when you discover work outside your current issue's scope.**

### When to Pause and Create an Issue

Ask yourself: "Is this required to complete my assigned issue?"

**If NO, stop and create an issue for:**
- Missing infrastructure (test frameworks, build tools, CI setup)
- Technical debt needing refactoring
- Missing features or improvements
- Documentation gaps
- Architecture changes or design improvements

**If YES, continue only if:**
- It's a prerequisite for your issue (e.g., can't write tests without test framework)
- It's a bug blocking your work
- It's explicitly mentioned in the issue requirements

### How to Handle Out-of-Scope Work

1. **PAUSE** - Stop implementing the out-of-scope work immediately
2. **ASSESS** - Determine if it's required for your current issue
3. **CREATE ISSUE** - If separate, create an unlabeled issue NOW (examples below)
4. **RESUME** - Return to your original task
5. **REFERENCE** - Mention the new issue in your PR if relevant

### When NOT to Create Issues

Don't create issues for:
- Minor code style fixes (just fix them in your PR)
- Already tracked TODOs
- Vague "nice to haves" without clear value
- Improvements you've already completed (document them in your PR instead)

### Example: Out-of-Scope Discovery

```bash
# While implementing feature, you discover missing test framework
# PAUSE: Stop trying to implement it
# CREATE: Make an issue for it

gh issue create --title "Add Vitest testing framework for frontend unit tests" --body "$(cat <<'EOF'
## Problem

While working on #38, discovered we cannot write unit tests for the state management refactor because no test framework is configured for the frontend.

## Requirements

- Add Vitest as dev dependency
- Configure vitest.config.ts
- Add test scripts to package.json
- Create example test to verify setup

## Context

Discovered during #38 implementation. Required for testing state management but separate concern from the refactor itself.
EOF
)"

# RESUME: Return to #38 implementation
```
