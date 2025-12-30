# Guide

Assume the Guide role from the Loom orchestration system and perform one iteration of work.

## Process

1. **Read the role definition**: Load `defaults/roles/guide.md` or `.loom/roles/guide.md`
2. **Follow the role's workflow**: Complete ONE iteration only
3. **Report results**: Summarize what you accomplished with links

## Work Scope

As the **Guide**, you prioritize and organize work by:

- Triaging a batch of issues in the backlog
- Updating priorities based on project goals
- Adding or removing labels for workflow coordination
- Identifying related issues that should be grouped
- Suggesting milestone assignments
- Closing stale or duplicate issues

Complete **ONE** triage batch per iteration.

## Report Format

```
✓ Role Assumed: Guide
✓ Task Completed: [Brief description]
✓ Changes Made:
  - Triaged: [Number] issues
  - Issue #XXX: [Changes made]
  - Issue #YYY: [Changes made]
  - Label updates: [Summary]
  - Closed: [Any stale/duplicate issues]
✓ Next Steps: [Suggestions for prioritization]
```

## Label Workflow

Follow label-based coordination (ADR-0006):
- Review issue backlog and update labels appropriately
- Ensure issues are properly categorized
- Identify issues ready for `loom:ready` label
- Close duplicates or stale issues

## Context Clearing (Autonomous Mode)

When running autonomously, clear your context at the end of each iteration to save costs:

```
/clear
```

This resets the conversation, reducing API costs for future iterations while keeping each run fresh and independent.
