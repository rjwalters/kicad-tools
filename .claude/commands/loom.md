# Assume Loom Role

Randomly select and assume an archetypal role from the Loom orchestration system, then perform one iteration of work following that role's guidelines.

## Process

1. **List available roles**: 9 roles available (builder, judge, curator, doctor, champion, architect, hermit, guide, driver)
2. **Select role using time-based selection**: Use `Date.now() % 13` for weighted random selection
3. **Role mapping** (core operational roles are double-weighted):
   ```
   0: builder    (2x weight)
   1: builder    (2x weight)
   2: judge      (2x weight)
   3: judge      (2x weight)
   4: curator    (2x weight)
   5: curator    (2x weight)
   6: doctor     (2x weight)
   7: doctor     (2x weight)
   8: champion   (1x weight)
   9: architect  (1x weight)
   10: hermit    (1x weight)
   11: guide     (1x weight)
   12: driver    (1x weight)
   ```
4. **Read the role definition**: Load `.loom/roles/<role>.md` or `defaults/roles/<role>.md`
5. **Follow the role's workflow**: Complete ONE iteration only (one task, one PR review, one issue triage, etc.)
6. **Report results**: Summarize what you accomplished with links to issues/PRs modified

## Available Roles

- **builder.md** - Claim `loom:issue` issue, implement feature/fix, create PR with `loom:review-requested`
- **judge.md** - Review PR with `loom:review-requested`, approve or request changes, update labels
- **curator.md** - Find unlabeled issue, enhance with technical details, mark as `loom:curated`
- **champion.md** - Evaluate `loom:curated` issues, promote to `loom:issue` or provide feedback
- **architect.md** - Create architectural proposal issue with `loom:architect` label
- **hermit.md** - Analyze codebase complexity, create bloat removal issue with `loom:hermit`
- **doctor.md** - Fix bug or address PR feedback, maintain existing PRs
- **guide.md** - Triage batch of issues, update priorities and labels for workflow
- **driver.md** - Execute direct task or command (plain shell, no specific workflow)

## Work Scope

Complete **ONE** meaningful task following the selected role's guidelines, then **stop and report**.

### Task Examples by Role

**Builder**: Claim one `loom:issue` issue → implement → test → commit → create PR
**Judge**: Review one PR with `loom:review-requested` → provide feedback → approve/request changes
**Curator**: Find one unlabeled issue → add context → tag as `loom:curated`
**Champion**: Evaluate `loom:curated` issues (max 2) → promote to `loom:issue` or provide feedback
**Architect**: Identify one architectural need → create detailed proposal issue
**Hermit**: Analyze codebase section → identify bloat → create removal issue
**Doctor**: Fix one bug or address one PR comment → update PR → push changes
**Guide**: Review issue backlog → update priorities → add/remove labels for batch
**Driver**: Execute one direct command or task per user instruction

## Report Format

After completing your iteration, report:

```
✓ Role Assumed: [Role Name]
✓ Task Completed: [Brief description]
✓ Changes Made:
  - [Issue/PR #XXX]: [Description with link]
  - [Label changes]
  - [Files modified if applicable]
✓ Next Steps: [Suggestions for follow-up or ready state]
```

## Label Workflow

Follow the label-based coordination system (ADR-0006):

- Issues: `loom:curated` → `loom:issue` → `loom:building` → closed
- PRs: `loom:review-requested` → `loom:pr` → merged
- Proposals: `loom:architect` → reviewed → implemented or closed
- Suggestions: `loom:hermit` → reviewed → implemented or closed

## Notes

- **Time-based selection**: Uses `Date.now() % 13` for deterministic but unpredictable role selection (no bash permissions needed!)
- **Weighted distribution**: Core operational roles (builder, judge, curator, doctor) are 2x more likely to be selected than supporting roles
- **Zero permissions**: No bash/python execution required - pure time-based mathematics
- This command simulates one terminal's work in the Loom multi-terminal orchestration system
- Multiple Claude Code sessions can run `/loom` in parallel for distributed work
- Each iteration should be atomic and complete (don't leave partial work)
- Always update labels appropriately to signal state to other roles
- Check for existing work to avoid conflicts (e.g., don't claim already `in-progress` issues)

## Example Usage

```bash
# In Claude Code terminal
/loom

# Claude responds:
"🎭 Selecting role using time-based weighted selection...
   Time index: Date.now() % 13 = 2
   Selected: Judge (2x weighted - core operational role)

Looking for PRs with loom:review-requested...
Found PR #401 - 'Add terminal restart functionality'

[Performs detailed review following judge.md guidelines]

✓ Role Assumed: Judge
✓ Task Completed: Reviewed PR #401
✓ Changes Made:
  - PR #401: Added detailed review comment with 3 suggestions
  - Label changed: loom:review-requested → loom:pr
  - Approved PR for merge
✓ Next Steps: Ready for maintainer to merge

Review complete!"
```
