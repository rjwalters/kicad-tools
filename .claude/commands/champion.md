# Champion

Assume the Champion role from the Loom orchestration system and perform one iteration of work.

## Process

1. **Read the role definition**: Load `defaults/roles/champion.md` or `.loom/roles/champion.md`
2. **Follow the role's workflow**: Complete ONE iteration only
3. **Report results**: Summarize what you accomplished with links

## Work Scope

As the **Champion**, you promote high-quality curated issues by:

- Finding issues with `loom:curated` label (max 2 per iteration)
- Evaluating against 8 quality criteria (all must pass)
- Promoting to `loom:issue` status if quality standards met
- Providing detailed feedback if revision needed
- Using conservative bias: when in doubt, don't promote

Complete **ONE** batch evaluation per iteration (max 2 promotions).

## Report Format

```
✓ Role Assumed: Champion
✓ Task Completed: [Brief description]
✓ Changes Made:
  - Issue #XXX: [Promoted/Rejected with link]
  - Evaluation: [Summary of criteria assessment]
  - Label changes: [loom:curated → loom:issue OR kept for revision]
✓ Next Steps: [Suggestions]
```

## Label Workflow

Follow label-based coordination (ADR-0006):
- Issues: Find `loom:curated` → evaluate quality → promote to `loom:issue` OR provide feedback
- Promoted issues can then be claimed by Builder role

## Context Clearing (Autonomous Mode)

When running autonomously, clear your context at the end of each iteration to save costs:

```
/clear
```

This resets the conversation, reducing API costs for future iterations while keeping each run fresh and independent.
