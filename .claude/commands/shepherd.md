# Loom

Assume the Loom orchestrator role from the Loom orchestration system and shepherd an issue through the full development lifecycle.

## Process

1. **Read the role definition**: Load `defaults/roles/loom.md` or `.loom/roles/loom.md`
2. **Parse the issue number**: Extract from arguments or prompt user
3. **Check dependencies**: Validate all issue dependencies are resolved (see Pre-Orchestration Dependency Check)
4. **Orchestrate the workflow**: Trigger roles in sequence with fresh context per phase
5. **Report results**: Summarize orchestration progress

## Work Scope

As the **Loom Orchestrator**, you coordinate the full issue lifecycle:

- **Curator phase**: Trigger curator terminal to enhance issue with implementation details
- **Approval gate**: Wait for `loom:issue` label (human or Champion approval)
- **Builder phase**: Trigger builder terminal to implement and create PR
- **Judge phase**: Trigger judge terminal to review the PR
- **Doctor loop**: If changes requested, trigger doctor (max 3 iterations)
- **Merge gate**: Wait for PR merge (Champion or human)

You don't do the work yourself - you orchestrate other terminals via MCP.

## Usage

```
/loom <issue-number>
/loom 123
/loom 456 --to curated    # Stop after curator phase
/loom 789 --resume        # Resume from last checkpoint
/loom 321 --force-pr      # Auto-approve, stop at reviewed PR
/loom 321 --force-merge   # Auto-approve, resolve conflicts, auto-merge
```

## Options

| Flag | Description |
|------|-------------|
| `--to <phase>` | Stop after specified phase (curated, pr, approved) |
| `--resume` | Resume from last checkpoint in issue comments |
| `--force-pr` | Auto-approve issue, run through Judge, stop at `loom:pr` state |
| `--force-merge` | Auto-approve, resolve merge conflicts, auto-merge after Judge approval |

### --force-pr Mode

When `--force-pr` is specified, the orchestrator:
1. **Skips Gate 1**: Auto-adds `loom:issue` label instead of waiting for human approval
2. **Stops at Gate 2**: Waits at `loom:pr` state for human to merge

Use `--force-pr` when you want automated development but want human approval on the final merge.

### --force-merge Mode

When `--force-merge` is specified, the orchestrator:
1. **Skips Gate 1**: Auto-adds `loom:issue` label instead of waiting for human approval
2. **Skips Gate 2**: Auto-merges the PR after Judge approval
3. **Resolves conflicts**: If merge conflicts exist, attempts to resolve them automatically

Use `--force-merge` when you want fully hands-off orchestration from issue to merged PR.

**Warning**: Force-merge mode will merge PRs without human review of the merge decision. The Judge still reviews code quality, but the final merge happens automatically.

## Phase Flow

```
Issue #N → Curator → [wait loom:curated] → [wait loom:issue]
        → Builder → [wait PR created] → Judge → [wait loom:pr or loom:changes-requested]
        → (Doctor loop if needed) → [wait merge] → Complete
```

## Terminal Requirements

Orchestration requires these terminals to be configured in the Loom app:

| Role | Purpose |
|------|---------|
| Curator | Enhance issue with implementation details |
| Builder | Implement feature, create PR |
| Judge | Review PR code quality |
| Doctor | Address review feedback (optional) |
| Champion | Auto-merge approved PRs (optional) |

## Report Format

```
✓ Role Assumed: Loom Orchestrator
✓ Issue: #XXX - [Title]
✓ Phases Completed:
  - Curator: ✅ (loom:curated)
  - Approval: ✅ (loom:issue)
  - Builder: ✅ (PR #YYY)
  - Judge: ✅ (loom:pr)
  - Merge: ✅ (merged)
✓ Status: Complete / Paused at [phase] / Blocked
✓ Duration: [time]
```

## Label Workflow

The Loom orchestrator monitors these label transitions:

**Issue labels**:
- `loom:curated` → Curator complete
- `loom:issue` → Approved for building

**PR labels**:
- `loom:review-requested` → PR ready for Judge
- `loom:pr` → Judge approved
- `loom:changes-requested` → Doctor needed

## MCP Integration

Loom uses MCP to control terminals:

```bash
# Restart terminal for fresh context
mcp__loom-terminals__restart_terminal --terminal_id terminal-2

# Configure phase-specific prompt
mcp__loom-terminals__configure_terminal \
  --terminal_id terminal-2 \
  --interval_prompt "Curate issue #123"

# Trigger immediate execution
mcp__loom-ui__trigger_run_now --terminalId terminal-2
```

## State Persistence

Progress is tracked in issue comments for crash recovery:

```markdown
<!-- loom:orchestrator
{"phase":"builder","iteration":0,"pr":456,"started":"2025-01-23T10:00:00Z"}
-->
```

On resume, the orchestrator reads this state and continues from the last phase.

## Pre-Orchestration Dependency Check

Before starting orchestration, `/loom` validates that all issue dependencies are resolved. This prevents wasted effort on issues that will inevitably block.

### Why Check Dependencies First?

Without pre-flight validation:
- Curator enhances an issue that can't be built yet
- API tokens wasted on orchestration that can't complete
- User sees false progress before discovering the block

### Dependency Patterns Recognized

The orchestrator scans the issue body for these patterns:

| Pattern | Example |
|---------|---------|
| Explicit blocker | `Blocked by #123` |
| Depends on | `Depends on #123` |
| Requires | `Requires #123` |
| Task list | `- [ ] #123: Description` |
| Dependencies section | `## Dependencies\n- #123` |

### How to Check Dependencies

```bash
# Parse issue body for dependency references
body=$(gh issue view "$issue_number" --json body --jq '.body')

# Extract issue numbers from dependency patterns
deps=$(echo "$body" | grep -oE '(Blocked by|Depends on|Requires|After|Parent.*#|\- \[.\] #)[0-9]+' | grep -oE '#[0-9]+' | tr -d '#' | sort -u)

# Check each dependency's state
for dep in $deps; do
  state=$(gh issue view "$dep" --json state --jq '.state')
  if [ "$state" != "CLOSED" ]; then
    echo "BLOCKED by #$dep ($state)"
  fi
done
```

### Behavior Without --force

If unresolved dependencies are found:

```
✓ Role Assumed: Loom Orchestrator
✓ Issue: #963 - [Parent #944] Part 2: Claim TTL and expiration cleanup

⚠️ Dependency Check Failed:
  - #962 (OPEN): Part 1: Atomic claiming system

Cannot proceed until dependencies are resolved.

Options:
  1. Wait for #962 to be completed
  2. Run with --force to attempt anyway (may block later)
  3. Run /loom 962 --force first to complete the dependency
```

### Behavior With --force

With `--force`, warn but continue:

```
✓ Role Assumed: Loom Orchestrator
✓ Issue: #963 - [Parent #944] Part 2: Claim TTL and expiration cleanup
✓ Mode: --force

⚠️ Unresolved Dependencies (proceeding anyway):
  - #962 (OPEN): Part 1: Atomic claiming system

Continuing with --force. Orchestration may block if dependencies are required.
```

### Checking PR Dependencies

For issues that depend on PRs (not just issues):

```bash
# Check if a PR is merged
pr_state=$(gh pr view "$pr_number" --json state,mergedAt --jq '.state')
# MERGED, OPEN, or CLOSED (without merge)
```

### Best Practices

1. **Always define dependencies explicitly** in issue body using recognized patterns
2. **Use task lists** for complex multi-part issues: `- [ ] #123: Part 1`
3. **Run dependencies first** with `/loom <dep-number> --force` before the dependent issue
4. **Check closed issues** - closed doesn't always mean merged (could be declined)
