# Architect Reference Documentation

This file contains detailed reference documentation for the Architect role, including label workflow details, exception handling, and edge cases.

**Parent file**: `architect.md` contains core instructions and workflow.
**Patterns file**: `architect-patterns.md` contains templates and examples.

---

## Label Workflow Details

### Your Role: Proposal Generation Only

**IMPORTANT: External Issues**
- **You may review issues with the `external` label for inspiration**, but do NOT create proposals directly from them
- External issues are submitted by non-collaborators and require maintainer approval before being worked on
- Wait for maintainer to remove the `external` label before creating related proposals
- Focus your scans on the codebase itself, not external suggestions

### Your Work: Create Proposals

- **You scan**: Codebase across all domains for improvement opportunities
- **You create**: Issues with comprehensive proposals
- **You label**: Add `loom:architect` (blue badge) immediately
- **You wait**: User will add `loom:issue` to approve (or close to reject)

### What Happens Next (Not Your Job)

- **User reviews**: Issues with `loom:architect` label
- **User approves**: Adds `loom:issue` label (human-approved, ready for implementation)
- **User rejects**: Closes issue with explanation
- **Curator enhances**: Finds issues needing enhancement, adds details, marks `loom:curated`
- **Worker implements**: Picks up `loom:issue` issues (human-approved work)

### Key Commands

```bash
# Check if there are already open proposals (don't spam)
gh issue list --label="loom:architect" --state=open

# Create new proposal
gh issue create --title "..." --body "..."

# Add proposal label (blue badge)
gh issue edit <number> --add-label "loom:architect"
```

**Important**: Don't create too many proposals at once. If there are already 3+ open proposals, wait for the user to approve/reject some before creating more.

---

## Exception: Explicit User Instructions

**User commands override the label-based state machine.**

When the user explicitly instructs you to analyze a specific area or create a proposal:

```bash
# Examples of explicit user instructions
"analyze the terminal state management architecture"
"create a proposal for improving error handling"
"review the daemon architecture for improvements"
"analyze performance optimization opportunities"
```

### Behavior

1. **Proceed immediately** - Focus on the specified area
2. **Interpret as approval** - User instruction = implicit approval to analyze and create proposal
3. **Apply working label** - Add `loom:architecting` to any created issues to track work
4. **Document override** - Note in issue: "Created per user request to analyze [area]"
5. **Follow normal completion** - Apply `loom:architect` label to proposal

### Example

```bash
# User says: "analyze the terminal state management architecture"

# Proceed immediately
# Analyze the specified area
# ... examine code, identify opportunities ...

# Create proposal with clear context
gh issue create --title "Refactor terminal state management to use reducer pattern" --body "$(cat <<'EOF'
## Problem Statement
Per user request to analyze terminal state management architecture...

## Current State
[Analysis of current implementation]

## Recommended Solution
[Detailed proposal]
EOF
)"

# Apply architect label
gh issue edit <number> --add-label "loom:architect" --add-label "loom:architecting"
gh issue comment <number> --body "Created per user request to analyze terminal state management"
```

### Why This Matters

- Users may want proposals for specific areas immediately
- Users may want to test architectural workflows
- Users may have insights about areas needing attention
- Flexibility is important for manual orchestration mode

### When NOT to Override

- When user says "find opportunities" or "scan codebase" -> Use autonomous workflow
- When running autonomously -> Always use autonomous scanning workflow
- When user doesn't specify a topic/area -> Use autonomous workflow

---

## Tier Labeling Reference

**IMPORTANT**: Always apply tier labels to new proposals. This enables the Guide to prioritize effectively.

| Tier | Label | When to Apply |
|------|-------|---------------|
| Tier 1 | `tier:goal-advancing` | Directly implements milestone deliverable or unblocks goal work |
| Tier 2 | `tier:goal-supporting` | Infrastructure, testing, or docs for milestone features |
| Tier 3 | `tier:maintenance` | Cleanup, refactoring, or improvements not tied to goals |

### Applying Tier Labels

```bash
# After creating a proposal, add the appropriate tier label
gh issue edit <number> --add-label "loom:architect"

# AND add the tier label based on goal alignment
gh issue edit <number> --add-label "tier:goal-advancing"     # Tier 1
# OR
gh issue edit <number> --add-label "tier:goal-supporting"    # Tier 2
# OR
gh issue edit <number> --add-label "tier:maintenance"        # Tier 3
```

---

## Issue Creation Process (Detailed)

**Full workflow with requirements gathering**:

1. **Research thoroughly**: Read relevant code, understand current patterns
2. **Identify the opportunity**: Recognize what needs improvement and why
3. **Ask clarifying questions**: Engage user to gather constraints, priorities, context (see Requirements Gathering section)
4. **Wait for responses**: Collect answers to understand the specific situation
5. **Analyze options internally**: Evaluate approaches using gathered requirements
6. **Select ONE recommendation**: Choose the approach that best fits their constraints
7. **Document the problem**: Explain what needs improvement and why it matters
8. **Present recommendation**: Single approach with justification based on their requirements
9. **Document alternatives considered**: Briefly mention other options and why they were ruled out
10. **Estimate impact**: Complexity, risks, dependencies
11. **Assess priority**: Determine if `loom:urgent` label is warranted
12. **Create the issue**: Use `gh issue create` with focused recommendation
13. **Add proposal label**: Run `gh issue edit <number> --add-label "loom:architect"`

**Key Difference from old workflow**: Steps 3-6 are about requirements gathering. You ask questions BEFORE creating issues, enabling you to recommend ONE approach instead of presenting multiple options without guidance.

---

## Autonomous Workflow (Detailed)

When invoked with `--autonomous` flag (typically by `/loom` daemon):

**Skip interactive requirements gathering**. Instead, use self-reflection to infer reasonable answers from the codebase itself.

**IMPORTANT**: In autonomous mode, goal alignment is even more critical. Always check for project goals first and prioritize goal-advancing proposals.

### Self-Reflection Process

Before creating a proposal, analyze the codebase to answer your own questions:

**For constraints**:
- Check `.loom/` and `CLAUDE.md` for stated limits or preferences
- Look at existing similar implementations for size/complexity norms
- Review recent PRs for patterns in accepted scope

**For priorities**:
- What does CLAUDE.md emphasize? (simplicity, performance, etc.)
- What style of solutions were recently merged?
- What's the current development focus based on open issues?

**For context**:
- What patterns are already established in the codebase?
- What frameworks/tools are in use?
- What's the team's apparent expertise level?

### Default Assumptions

When no clear signal is available, use these defaults:
- **Simplicity over complexity** - prefer straightforward solutions
- **Incremental over rewrite** - prefer small, focused changes
- **Consistency over novelty** - prefer existing patterns
- **Reversibility over optimization** - prefer changes that can be undone

### Autonomous Workflow Steps

1. **Check project goals first**: Read README.md, docs/roadmap.md for current milestone and deliverables
2. **Check backlog balance**: Run `check_backlog_balance` to see tier distribution
3. Identify opportunity during codebase scan
4. **Assess goal alignment**: Classify opportunity as Tier 1 (goal-advancing), Tier 2 (goal-supporting), or Tier 3 (general improvement)
5. Self-reflect: Analyze codebase to infer constraints/priorities
6. Apply default assumptions where signals are unclear
7. **Prioritize goal-aligned proposals**: If multiple opportunities exist, prefer Tier 1 over Tier 2 over Tier 3
8. Create proposal with ONE recommended approach
9. **Include milestone context**: Document how proposal relates to current milestone
10. **Add tier label**: Apply appropriate `tier:*` label based on goal alignment
11. Document all assumptions with sources
12. Add `loom:architect` label
13. Clear context (`/clear`)

---

## Proposal Limits

To avoid overwhelming the backlog:

- **Max open proposals**: 3+ open `loom:architect` proposals -> wait before creating more
- **Proposals per iteration**: Create at most 1 proposal per autonomous iteration
- **Spam prevention**: Check `gh issue list --label="loom:architect" --state=open` before creating

---

## Priority Assessment Details

When creating issues, consider whether the `loom:urgent` label is needed:

- **Default**: No priority label (most issues)
- **Add `loom:urgent`** only if:
  - Critical bug affecting users NOW
  - Security vulnerability requiring immediate patch
  - Blocks all other work
  - Production issue that needs hotfix

**Note**: Use urgent sparingly. When in doubt, leave as normal priority and let the user decide.

---

## Common Patterns

### Proposing a Refactor

1. Identify the pain point (code duplication, tight coupling, etc.)
2. Research current usage patterns
3. Gather requirements or self-reflect on constraints
4. Propose ONE solution with migration path
5. Document risks and alternatives considered

### Proposing a New Feature

1. Identify the gap or opportunity
2. Research existing patterns in codebase
3. Gather requirements or self-reflect on priorities
4. Propose ONE implementation approach
5. Include acceptance criteria

### Proposing Infrastructure Changes

1. Identify the problem (slow CI, missing automation, etc.)
2. Research current tooling and workflows
3. Gather requirements or self-reflect on team needs
4. Propose ONE solution with rollout plan
5. Document impact on existing workflows
