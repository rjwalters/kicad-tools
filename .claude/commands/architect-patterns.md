# Architect Patterns and Templates

This file contains detailed templates, examples, and patterns for creating architectural proposals. Load this file when you need to create proposals or epics.

**Parent file**: `architect.md` contains core instructions and workflow.

---

## Issue Template

Use this template when creating architectural proposals:

```markdown
## Problem Statement

Describe the architectural issue or opportunity. Why does this matter?

## Milestone Alignment

**Current Milestone**: [e.g., "M0 - Bootstrap" or "unknown if no roadmap found"]
**Alignment Tier**: [Tier 1 - Goal-Advancing | Tier 2 - Goal-Supporting | Tier 3 - General Improvement]
**Related Deliverable**: [If Tier 1/2, which specific deliverable does this advance?]
**Justification**: [Why this proposal matters for the current milestone]

## Current State

How does the system work today? What are the pain points?

## Requirements Gathered

Summarize the key constraints, priorities, and context from user responses:
- **Constraint**: [e.g., "500MB storage budget"]
- **Priority**: [e.g., "Simplicity over performance"]
- **Context**: [e.g., "Weekly re-analysis pattern"]
- **Existing**: [e.g., "Already using Redis for session storage"]

## Recommended Solution

**Approach**: [Single recommended approach name and brief description]

**Why This Approach**:
- Fits constraint: [How it addresses their specific constraint]
- Aligns with priority: [How it matches their stated priority]
- Matches context: [How it fits their usage pattern]
- Integrates well: [How it works with existing systems]

**Implementation**:
- [Key implementation steps or components]
- [Technical details relevant to this approach]

**Complexity**: Estimate (Low/Medium/High with brief justification)

**Dependencies**: Related issues or prerequisites (if any)

## Alternatives Considered

Briefly document other options you evaluated and why they were ruled out:

**[Alternative 1]**: [Why it doesn't fit] (e.g., "Manual invalidation - doesn't match preference for automatic cleanup")

**[Alternative 2]**: [Why it doesn't fit] (e.g., "LRU eviction - poor cache hit ratio for their access patterns")

## Impact

- **Files affected**: Rough estimate
- **Breaking changes**: Yes/No
- **Migration path**: How to transition
- **Risks**: What could go wrong

## Related

- Links to related issues, PRs, docs
- References to similar patterns in other projects
```

### Template Notes

- **NEW**: "Milestone Alignment" section shows how proposal advances project goals
- **NEW**: "Requirements Gathered" section shows you listened and understood
- **Focus**: Single actionable recommendation instead of "choose one of these"
- **Priority**: Goal-advancing proposals weighted higher than general improvements

---

## Issue Creation Command

```bash
# Create proposal issue
gh issue create --title "..." --body "$(cat <<'EOF'
[issue content here]
EOF
)"

# Add proposal label (blue badge - awaiting user approval)
gh issue edit <number> --add-label "loom:architect"

# Add tier label based on goal alignment
gh issue edit <number> --add-label "tier:goal-advancing"     # Tier 1
# OR
gh issue edit <number> --add-label "tier:goal-supporting"    # Tier 2
# OR
gh issue edit <number> --add-label "tier:maintenance"        # Tier 3
```

---

## Autonomous Mode Assumptions Template

When creating proposals in autonomous mode, include this section to document your assumptions:

```markdown
## Autonomous Mode Assumptions

This proposal was created in autonomous mode. The following assumptions were made:

| Question | Inferred Answer | Source |
|----------|-----------------|--------|
| Current milestone? | M0 - Bootstrap | README.md milestone section |
| Alignment tier? | Tier 1 - Goal-Advancing | Directly implements M0 deliverable |
| Priority? | Simplicity | CLAUDE.md emphasizes maintainability |
| Breaking changes? | Minimize | Recent PRs favor incremental changes |
| Pattern preference? | Shared crates | Existing loom-db, loom-types pattern |

**Goal alignment note**: This proposal advances the "[deliverable]" objective for [milestone].

**Reviewer note**: Please validate these assumptions match your actual preferences.
```

---

## Requirements Gathering Examples

### For Caching Decisions

Questions to ask:
- "What's your storage budget for cached data?"
- "How often do users re-access the same resources?"
- "Do you prefer automatic cleanup or manual control?"
- "What's the expected cache size and growth rate?"

### For Architecture Decisions

Questions to ask:
- "What's your priority: simplicity or performance?"
- "What's the expected request volume and concurrency?"
- "Are there existing patterns we should follow for consistency?"
- "What's the team's familiarity with different architectural styles?"

### For Refactoring Decisions

Questions to ask:
- "What's the most painful part of the current implementation?"
- "How much risk tolerance do you have for breaking changes?"
- "What's the timeline for this improvement?"
- "Are there other teams depending on the current API?"

### Example Engagement

```
I've identified an opportunity to add caching for analysis results in StyleCheck. Before I create a proposal, I need to understand a few things:

1. What's your storage budget for cached data? (unlimited, 500MB, 100MB, etc.)
2. How often do users re-analyze the same files? (every commit, weekly, rarely)
3. Do you prefer automatic cache invalidation or manual refresh controls?
4. What's more important: maximizing cache hit ratio or minimizing storage use?

Your answers will help me recommend the most appropriate caching strategy.
```

---

## Epic Proposals

For large features that span multiple phases or require coordinated implementation, create an **Epic** instead of a single issue.

### When to Create an Epic

Create an epic when:
- Feature requires 4+ distinct implementation issues
- Work has natural phases with dependencies between them
- Multiple shepherds could work on different parts in parallel
- Feature spans multiple subsystems or architectural layers
- Implementation order matters (foundation before features)

**Don't create an epic when:**
- Feature can be implemented in a single PR
- Work is straightforward with no phase dependencies
- Changes are isolated to one subsystem

### Epic Template

```markdown
# Epic: [Title]

## Overview

[High-level description of the multi-phase feature. What problem does it solve?
Why is this being built as an epic rather than individual issues?]

## Milestone Alignment

**Current Milestone**: [e.g., "M1 - Core Features"]
**Alignment Tier**: [Tier 1 - Goal-Advancing | Tier 2 - Goal-Supporting]
**Justification**: [How this epic advances the project roadmap]

## Phases

### Phase 1: [Foundation]
**Goal**: [What this phase accomplishes]
**Can parallelize**: [Yes/No - can issues within this phase run in parallel?]

- [ ] Issue A: [Brief description - enough for Builder to understand scope]
- [ ] Issue B: [Brief description]

### Phase 2: [Core Implementation]
**Blocked by**: Phase 1
**Goal**: [What this phase accomplishes]
**Can parallelize**: [Yes/No]

- [ ] Issue C: [Brief description]
- [ ] Issue D: [Brief description]

### Phase 3: [Polish/Integration]
**Blocked by**: Phase 2
**Goal**: [What this phase accomplishes]

- [ ] Issue E: [Brief description]

## Success Criteria

How do we know this epic is complete?
- [ ] [Measurable outcome 1]
- [ ] [Measurable outcome 2]

## Risks & Considerations

- [Risk 1 and mitigation]
- [Risk 2 and mitigation]

## Complexity Estimate

| Phase | Complexity | Est. Issues |
|-------|------------|-------------|
| Phase 1 | Low/Medium/High | N |
| Phase 2 | Low/Medium/High | N |
| Phase 3 | Low/Medium/High | N |
| **Total** | | N |
```

### Creating an Epic

```bash
# Create epic issue
gh issue create --title "Epic: [Title]" --body "$(cat <<'EOF'
[epic content using template above]
EOF
)"

# Add epic label (NOT loom:architect)
gh issue edit <number> --add-label "loom:epic"
```

**Important**: Use `loom:epic` label, not `loom:architect`. Epics follow a different approval workflow.

### Epic Workflow

```
Architect creates epic -> loom:epic label
        |
Champion evaluates epic structure
        |
Champion approves -> Creates Phase 1 issues with loom:architect
        |
Phase 1 issues get loom:issue -> Shepherds implement
        |
Phase 1 completes -> Champion creates Phase 2 issues
        |
... repeat until all phases complete ...
        |
Epic issue closed
```

### Phase Issue Creation

When Champion approves an epic, Phase 1 issues are created with:
- `loom:architect` label (awaiting individual approval)
- `loom:epic-phase` label (indicates part of epic)
- Body includes: `**Epic**: #[epic-number] | **Phase**: 1`
- Dependencies reference the epic: `Blocked by: Epic #[number] approval`

### Example Epic

```markdown
# Epic: Implement Agent Performance Metrics System

## Overview

Add comprehensive performance tracking for Loom agents, enabling self-aware
behavior where agents can check their own success rates and adjust strategies.

## Milestone Alignment

**Current Milestone**: M2 - Observability
**Alignment Tier**: Tier 1 - Goal-Advancing
**Justification**: Directly implements the "agent self-monitoring" M2 deliverable.

## Phases

### Phase 1: Data Collection
**Goal**: Capture raw performance data from agent operations
**Can parallelize**: Yes

- [ ] Add metrics schema to daemon-state.json
- [ ] Capture prompt counts and token usage per role
- [ ] Track issue completion success/failure

### Phase 2: Aggregation & Storage
**Blocked by**: Phase 1
**Goal**: Process and store metrics for querying
**Can parallelize**: Yes

- [ ] Add metrics aggregation on daemon iteration
- [ ] Implement time-windowed statistics (hourly, daily, weekly)
- [ ] Add MCP tool: get_agent_metrics

### Phase 3: Agent Integration
**Blocked by**: Phase 2
**Goal**: Enable agents to use their own metrics
**Can parallelize**: No

- [ ] Add agent-metrics.sh CLI script
- [ ] Document self-aware agent patterns
- [ ] Add escalation triggers based on success rate

## Success Criteria

- [ ] Agents can query their own success rate
- [ ] Metrics visible in daemon status reports
- [ ] Documented pattern for metric-based escalation

## Risks & Considerations

- Performance overhead of metrics collection
- Privacy of metrics data (no PII in metrics)

## Complexity Estimate

| Phase | Complexity | Est. Issues |
|-------|------------|-------------|
| Phase 1 | Low | 3 |
| Phase 2 | Medium | 3 |
| Phase 3 | Low | 3 |
| **Total** | Medium | 9 |
```

### Guidelines for Epics

- **Keep phases small**: 2-4 issues per phase is ideal
- **Clear dependencies**: Each phase should have explicit blockers
- **Parallelizable work**: Note which issues within a phase can run in parallel
- **Standalone issues**: Each issue should be implementable without epic context
- **Progress tracking**: Champion updates epic checkboxes as issues complete
- **Don't over-epic**: Simple features don't need epic structure

---

## Tracking Dependencies with Task Lists

When an issue depends on other issues being completed first, use GitHub task lists to make dependencies explicit and trackable.

### When to Add Dependencies

Add a Dependencies section if:
- Issue requires prerequisite work from other issues
- Implementation must wait for infrastructure/framework to be in place
- Issue is part of a multi-phase feature with sequential steps

### Task List Format

```markdown
## Dependencies

- [ ] #123: Brief description of what's needed
- [ ] #456: Another prerequisite issue

This issue cannot proceed until all dependencies above are complete.
```

### Benefits of Task Lists

- GitHub automatically checks boxes when issues close
- Visual progress indicator in issue cards
- Clear "ready to start" signal when all boxes checked
- Curator can programmatically check completion status

### Example: Multi-Phase Feature

```markdown
## Dependencies

**Phase 1 (must complete first):**
- [ ] #100: Database migration system
- [ ] #101: Add users table schema

**Phase 2 (current):**
- This issue implements user authentication

This issue requires the users table from Phase 1.
```

### Guidelines

- Use task lists for blocking dependencies only (not nice-to-haves)
- Keep dependency descriptions brief but clear
- Mention why the dependency exists if not obvious
- For independent work, explicitly state "No dependencies"

---

## Goal Discovery Script

Full script for discovering project goals:

```bash
discover_project_goals() {
  echo "=== Project Goals Discovery ==="

  # 1. Check README for milestones
  if [ -f README.md ]; then
    echo "Current milestone from README:"
    grep -i "milestone\|current:\|target:" README.md | head -5
  fi

  # 2. Check roadmap
  if [ -f docs/roadmap.md ] || [ -f ROADMAP.md ]; then
    echo "Roadmap deliverables:"
    grep -E "^- \[.\]|^## M[0-9]" docs/roadmap.md ROADMAP.md 2>/dev/null | head -10
  fi

  # 3. Check for urgent/high-priority goal-advancing issues
  echo "Current goal-advancing work:"
  gh issue list --label="tier:goal-advancing" --state=open --limit=5
  gh issue list --label="loom:urgent" --state=open --limit=5

  # 4. Summary
  echo "Goal-advancing proposals should target these areas"
}

# Run goal discovery
discover_project_goals

# Additional checks
ls -la README.md docs/roadmap.md docs/milestones/ 2>/dev/null
grep -i "milestone\|deliverable\|goal\|phase" README.md | head -20
gh issue list --label="milestone:*" --state=open --limit=10
```

---

## Backlog Balance Check Script

Full script for checking backlog balance:

```bash
check_backlog_balance() {
  echo "=== Backlog Tier Balance ==="

  # Count issues by tier
  tier1=$(gh issue list --label="tier:goal-advancing" --state=open --json number --jq 'length')
  tier2=$(gh issue list --label="tier:goal-supporting" --state=open --json number --jq 'length')
  tier3=$(gh issue list --label="tier:maintenance" --state=open --json number --jq 'length')
  unlabeled=$(gh issue list --label="loom:issue" --state=open --json number,labels \
    --jq '[.[] | select([.labels[].name] | any(startswith("tier:")) | not)] | length')

  total=$((tier1 + tier2 + tier3 + unlabeled))

  echo "Tier 1 (goal-advancing): $tier1"
  echo "Tier 2 (goal-supporting): $tier2"
  echo "Tier 3 (maintenance):     $tier3"
  echo "Unlabeled:                $unlabeled"
  echo "Total ready issues:       $total"

  # Check balance
  if [ "$tier1" -eq 0 ] && [ "$total" -gt 3 ]; then
    echo ""
    echo "WARNING: No goal-advancing issues in backlog!"
    echo "RECOMMENDATION: Prioritize creating Tier 1 proposals that advance current milestone."
  fi

  if [ "$tier3" -gt "$tier1" ] && [ "$tier3" -gt 5 ]; then
    echo ""
    echo "WARNING: More maintenance issues than goal-advancing issues."
    echo "RECOMMENDATION: Focus on goal-aligned proposals before adding more maintenance work."
  fi
}

# Run the check
check_backlog_balance
```

**Interpretation**:
- **Healthy**: Tier 1 >= Tier 3, and at least 1-2 goal-advancing issues available
- **Warning**: No goal-advancing issues, or maintenance dominates
- **Action**: If unhealthy, focus proposals on Tier 1 opportunities

---

## Milestone Alignment Examples

### Goal-Aligned Example (Tier 1)

```markdown
## Milestone Alignment

**Current Milestone**: M0 - Bootstrap
**Deliverable**: "Basic window opens via platform crate"
**Alignment**: This proposal directly implements the window creation deliverable.
```

### Non-Goal-Aligned Example (Tier 3)

```markdown
## Milestone Alignment

**Current Milestone**: M0 - Bootstrap
**Alignment**: Infrastructure improvement (not a direct M0 deliverable)
**Justification**: While not directly advancing M0, this CI setup will catch build failures early and is listed as an M0 deliverable.
```

### Goal-Aligned vs General Examples

**Project at M0 (Bootstrap) with deliverables:**
- [ ] Workspace builds successfully
- [ ] CI pipeline runs
- [ ] Basic window opens via platform crate
- [ ] "Hello rect" render path works

**Goal-Advancing Proposals (Tier 1):**
- "Implement basic window creation in vw-platform" - directly advances M0
- "Add hello rect render path in vw-gfx" - directly advances M0

**Goal-Supporting Proposals (Tier 2):**
- "Add CI pipeline for Rust builds" - listed in M0 but infrastructure

**General Improvement Proposals (Tier 3):**
- "Consolidate cleanup scripts" - valid but not goal-advancing
- "Add comprehensive error handling" - good but not urgent for M0
