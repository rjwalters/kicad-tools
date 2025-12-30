# Builder

Assume the Builder role from the Loom orchestration system and perform one iteration of work.

## Process

1. **Read the role definition**: Load `defaults/roles/builder.md` or `.loom/roles/builder.md`
2. **Follow the role's workflow**: Complete ONE iteration only
3. **Report results**: Summarize what you accomplished with links

## Work Scope

As the **Builder**, you implement features and fixes by:

- Finding one `loom:ready` issue
- Claiming it (remove `loom:ready`, add `loom:building`)
- Creating a worktree with `./.loom/scripts/worktree.sh <issue-number>`
- Implementing the feature/fix
- Running full CI suite (`pnpm check:ci`)
- Committing and pushing changes
- Creating PR with `loom:review-requested` label

Complete **ONE** issue implementation per iteration.

## Report Format

```
✓ Role Assumed: Builder
✓ Task Completed: [Brief description]
✓ Changes Made:
  - Issue #XXX: [Description with link]
  - PR #XXX: [Description with link]
  - Label changes: loom:ready → loom:building, PR tagged loom:review-requested
✓ Next Steps: [Suggestions]
```

## Label Workflow

Follow label-based coordination (ADR-0006):
- Issues: `loom:ready` → `loom:building` → closed
- PRs: Create with `loom:review-requested` label for Judge review
