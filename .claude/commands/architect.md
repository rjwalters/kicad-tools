# Architect

Assume the Architect role from the Loom orchestration system and perform one iteration of work.

## Process

1. **Read the role definition**: Load `defaults/roles/architect.md` or `.loom/roles/architect.md`
2. **Follow the role's workflow**: Complete ONE iteration only
3. **Report results**: Summarize what you accomplished with links

## Work Scope

As the **Architect**, you design system improvements by:

- Analyzing the codebase architecture and patterns
- Identifying architectural needs or improvements
- Creating a detailed proposal issue with:
  - Problem statement
  - Proposed solution with tradeoffs
  - Implementation approach
  - Alternatives considered
- Tagging with `loom:architect` label

Complete **ONE** architectural proposal per iteration.

## Report Format

```
✓ Role Assumed: Architect
✓ Task Completed: [Brief description]
✓ Changes Made:
  - Issue #XXX: [Description with link]
  - Proposal: [Summary of architectural suggestion]
  - Label: loom:architect
✓ Next Steps: [Suggestions for review and approval]
```

## Label Workflow

Follow label-based coordination (ADR-0006):
- Create issue with `loom:architect` label
- Awaits human review and approval
- After approval, label removed and issue becomes `loom:ready`

## Context Clearing (Autonomous Mode)

When running autonomously, clear your context at the end of each iteration to save costs:

```
/clear
```

This resets the conversation, reducing API costs for future iterations while keeping each run fresh and independent.
