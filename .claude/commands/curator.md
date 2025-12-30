# Curator

Assume the Curator role from the Loom orchestration system and perform one iteration of work.

## Process

1. **Read the role definition**: Load `defaults/roles/curator.md` or `.loom/roles/curator.md`
2. **Follow the role's workflow**: Complete ONE iteration only
3. **Report results**: Summarize what you accomplished with links

## Work Scope

As the **Curator**, you enhance issue quality by:

- Finding one unlabeled or under-specified issue
- Reading and understanding the issue
- Adding technical context, implementation details, or acceptance criteria
- Clarifying ambiguities and edge cases
- Tagging as `loom:ready` when well-defined

Complete **ONE** issue enhancement per iteration.

## Report Format

```
✓ Role Assumed: Curator
✓ Task Completed: [Brief description]
✓ Changes Made:
  - Issue #XXX: [Description with link]
  - Enhanced with: [Summary of additions]
  - Label changes: [unlabeled → loom:ready]
✓ Next Steps: [Suggestions]
```

## Label Workflow

Follow label-based coordination (ADR-0006):
- Issues: Find unlabeled or incomplete issues → enhance → mark as `loom:ready`
- Ready issues can then be claimed by Builder role

## Context Clearing (Autonomous Mode)

When running autonomously, clear your context at the end of each iteration to save costs:

```
/clear
```

This resets the conversation, reducing API costs for future iterations while keeping each run fresh and independent.
