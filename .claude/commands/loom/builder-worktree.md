# Builder: Worktree Workflows

This document covers git worktree management for the Builder role. For the core builder workflow, see `builder.md`.

## On-Demand Git Worktrees

When working on issues, you should **create worktrees on-demand** to isolate your work. This prevents conflicts and allows multiple agents to work simultaneously.

### IMPORTANT: Use the Worktree Helper Script

**Always use `./.loom/scripts/worktree.sh <issue-number>` to create worktrees.** This helper script ensures:
- Correct path (`.loom/worktrees/issue-{number}`)
- Prevents nested worktrees
- Consistent branch naming
- Sandbox compatibility

```bash
# CORRECT - Use the helper script
./.loom/scripts/worktree.sh 84

# WRONG - Don't use git worktree directly
git worktree add .loom/worktrees/issue-84 -b feature/issue-84 main
```

### Why This Matters

1. **Prevents Nested Worktrees**: Helper detects if you're already in a worktree and prevents double-nesting
2. **Sandbox-Compatible**: Worktrees inside `.loom/worktrees/` stay within workspace
3. **Gitignored**: `.loom/worktrees/` is already gitignored
4. **Consistent Naming**: `issue-{number}` naming matches GitHub issues
5. **Safety Checks**: Validates issue numbers, checks for existing directories

### Worktree Workflow Example

```bash
# 1. Claim an issue
gh issue edit 84 --remove-label "loom:issue" --add-label "loom:building"

# 2. Create worktree using helper
./.loom/scripts/worktree.sh 84
# -> Creates: .loom/worktrees/issue-84
# -> Branch: feature/issue-84

# 3. Capture the worktree ABSOLUTE path ONCE (see warning below)
WORKTREE_ABS="$(cd .loom/worktrees/issue-84 && pwd)"
# -> e.g. /Users/you/repo/.loom/worktrees/issue-84

# 4. Do your work using ABSOLUTE paths (implement, test, commit)
#    - Write/Edit: pass "$WORKTREE_ABS/<file>"
#    - Bash:       git -C "$WORKTREE_ABS" ...  OR  cd "$WORKTREE_ABS" && <cmd>
# ... work work work ...

# 5. Push and create PR from the worktree
git -C "$WORKTREE_ABS" push -u origin feature/issue-84
gh pr create --label "loom:review-requested"

# 6. Worktree cleanup is automatic - DO NOT manually delete worktrees
# Worktrees are cleaned up automatically when PRs merge or by loom-clean
```

### CRITICAL: `cd` Does NOT Persist Across Tool Calls

**The harness resets your working directory between tool calls.** A `cd
.loom/worktrees/issue-N` in one Bash call does **not** carry over to the next
Write, Edit, or Bash call — the next call starts back at the main repo root.
If you rely on a persisted `cd` and then use a repo-relative path, your file
operation lands in the **main worktree** instead of your issue worktree,
silently contaminating main (#3513, recurrence of #2802).

**Do this instead:**

1. Capture the worktree's absolute path **once**, right after creating it:
   ```bash
   WORKTREE_ABS="$(cd .loom/worktrees/issue-N && pwd)"
   ```
2. Use absolute paths for **every** file-mutating operation thereafter:
   - **Write / Edit tools** — pass the full path `"$WORKTREE_ABS/path/to/file"`.
     These tools have no cwd; the path you give is the path written.
   - **Bash** — either re-assert `cd "$WORKTREE_ABS" &&` at the **start of each
     file-mutating invocation**, or use `git -C "$WORKTREE_ABS" ...` and
     absolute paths. Never assume an earlier `cd` is still in effect.
3. Before committing, verify your changes are in the worktree and main is clean:
   ```bash
   git -C "$WORKTREE_ABS" status        # changes should be HERE
   ./.loom/scripts/check-main-clean.sh  # backstop: exits 3 if main is dirty
   ```

### Collision Detection

The worktree helper script prevents common errors:

```bash
# If you're already in a worktree
./.loom/scripts/worktree.sh 84
# -> ERROR: You are already in a worktree!
# -> Instructions to return to main before creating new worktree

# If directory already exists
./.loom/scripts/worktree.sh 84
# -> Checks if it's a valid worktree or needs cleanup
```

### Working Without Worktrees

**You start in the main workspace.** Only create a worktree when you claim an issue and need isolation:

- **NO worktree needed**: Browsing code, reading files, checking status
- **CREATE worktree**: When claiming an issue and starting implementation

This on-demand approach prevents worktree clutter and reduces resource usage.

## Handling Merge Conflicts in Worktrees

When your feature branch has conflicts with main, you have two options depending on the severity of divergence.

### Option 1: Resolve Conflicts in the Worktree (Recommended)

For minor conflicts or small divergence from main:

```bash
# In the worktree directory (.loom/worktrees/issue-XX)
git fetch origin main
git rebase origin/main

# If conflicts occur:
# 1. Edit conflicting files to resolve
# 2. Stage resolved files
git add <resolved-files>

# 3. Continue the rebase
git rebase --continue

# 4. Force push (rebase rewrites history)
git push --force-with-lease
```

**When to use this approach:**
- Few files have conflicts
- Conflicts are straightforward to resolve
- Your changes are relatively small

### Option 2: Create a Fresh Worktree (For Significant Divergence)

If conflicts are too complex or main has changed significantly:

```bash
# 1. Save your work (note what you changed)
git diff HEAD > ~/my-changes.patch  # Optional: save as patch

# 2. Return to main repository (not the worktree)
cd /path/to/main/repo

# 3. Remove the stale worktree
git worktree remove .loom/worktrees/issue-XX --force

# 4. Delete the old branch
git branch -D feature/issue-XX

# 5. Fetch latest main
git fetch origin main

# 6. Create fresh worktree from updated main
./.loom/scripts/worktree.sh XX

# 7. Re-implement changes in the fresh worktree
cd .loom/worktrees/issue-XX
# Cherry-pick, apply patch, or reimplement manually
```

**When to use this approach:**
- Many files have conflicts
- Main has diverged significantly (many commits ahead)
- Conflicts are in areas you didn't intentionally change
- Easier to reimplement than untangle

### Never Do This

**Don't delete worktrees manually with `git worktree remove`**
- Running `git worktree remove` while your shell is in the worktree corrupts shell state
- Even `pwd` will fail with "No such file or directory" errors
- Use `loom-clean` for safe cleanup (handles edge cases)
- Worktrees auto-cleanup when PRs merge

**Don't switch to the main repository directory to work on features**
- Always work in worktrees for isolation
- Main should stay clean and on the default branch

**Don't create branches directly in main**
- Always use `./.loom/scripts/worktree.sh` to create branches
- Prevents nested worktree issues

**Don't run `git stash` in main and try to apply in worktrees**
- Stash is local to the repository, not shared between worktrees
- Each worktree has its own working directory

**Don't use `git push --force` without `--force-with-lease`**
- `--force-with-lease` is safer - it fails if someone else pushed
- Prevents accidentally overwriting others' work

### Preventing Merge Conflicts

To minimize conflicts in the first place:

1. **Pull frequently**: Before starting significant work
   ```bash
   git fetch origin main
   git rebase origin/main
   ```

2. **Keep PRs small**: Smaller PRs = fewer conflicts = faster merges

3. **Communicate**: If working on shared areas, coordinate with other builders

4. **Rebase before PR**: Always rebase onto latest main before creating PR
   ```bash
   git fetch origin main
   git rebase origin/main
   git push --force-with-lease
   ```

## Claiming Workflow (Parallel Mode)

When working with parallel agents (multiple Builders running simultaneously), use the atomic claiming system to prevent race conditions.

### Why Use Atomic Claims?

**The Problem with Labels Alone:**
- Two Builders see `loom:issue` at the same time
- Both try to claim by adding `loom:building`
- Race condition: both may succeed, causing duplicate work

**The Solution:**
- Use `loom-claim` for atomic file-based locking
- Label change is still needed (for visibility), but loom-claim prevents races
- First Builder to claim wins; others move to next issue

### Claiming Workflow

**1. Check for stop signals before claiming new work:**

```bash
# Check if stop signal exists (graceful shutdown)
if ./.loom/scripts/signal.sh check "$AGENT_ID"; then
  echo "Stop signal received, completing current work and exiting"
  exit 0
fi
```

**2. Attempt atomic claim before label change:**

```bash
# Try to claim atomically (prevents race conditions)
if loom-claim claim "$ISSUE_NUMBER" "$AGENT_ID"; then
  # Claim succeeded - now update labels for visibility
  gh issue edit "$ISSUE_NUMBER" --remove-label "loom:issue" --add-label "loom:building"
  echo "Claimed issue #$ISSUE_NUMBER"
else
  # Another agent claimed it first
  echo "Issue #$ISSUE_NUMBER already claimed, trying next issue"
  continue  # In a loop, move to next issue
fi
```

**3. Extend claim for long-running work:**

```bash
# If work takes longer than 30 minutes, extend the claim
loom-claim extend "$ISSUE_NUMBER" "$AGENT_ID" 3600  # Extend by 1 hour
```

**4. Release claim on completion or abandonment:**

```bash
# When PR is created or work is blocked, release the claim
loom-claim release "$ISSUE_NUMBER" "$AGENT_ID"
```

### Full Parallel Mode Example

```bash
#!/bin/bash
AGENT_ID="${AGENT_ID:-builder-$$}"

while true; do
  # Check for stop signal
  if ./.loom/scripts/signal.sh check "$AGENT_ID"; then
    echo "Stop signal received, exiting"
    exit 0
  fi

  # Find available issues
  ISSUES=$(gh issue list --label="loom:issue" --state=open --json number --jq '.[].number')

  for ISSUE_NUMBER in $ISSUES; do
    # Try atomic claim
    if loom-claim claim "$ISSUE_NUMBER" "$AGENT_ID" 1800; then
      # Claim succeeded
      gh issue edit "$ISSUE_NUMBER" --remove-label "loom:issue" --add-label "loom:building"

      # Create worktree and do work
      ./.loom/scripts/worktree.sh "$ISSUE_NUMBER"
      cd ".loom/worktrees/issue-$ISSUE_NUMBER"

      # ... implement feature ...
      # ... run tests ...
      # ... create PR ...

      # Release claim (PR will handle the rest)
      loom-claim release "$ISSUE_NUMBER" "$AGENT_ID"

      break  # Move to next iteration
    fi
  done

  # No issues available, wait before retrying
  sleep 60
done
```

### Claim Commands Reference

| Command | Purpose |
|---------|---------|
| `loom-claim claim <issue> [agent-id] [ttl]` | Atomically claim an issue (default TTL: 30 min) |
| `loom-claim extend <issue> <agent-id> [seconds]` | Extend claim TTL for long work |
| `loom-claim release <issue> [agent-id]` | Release claim when done |
| `loom-claim check <issue>` | Check if issue is claimed |
| `loom-claim list` | List all active claims |
| `loom-claim cleanup` | Remove expired claims |

### Signal Commands Reference

| Command | Purpose |
|---------|---------|
| `signal.sh stop <agent-id\|all>` | Send stop signal |
| `signal.sh check <agent-id>` | Check for stop signal (exit 0 if signal exists) |
| `signal.sh clear <agent-id\|all>` | Clear stop signal |

### When to Use Parallel Mode

**Use atomic claiming when:**
- Multiple Builder agents run simultaneously (Daemon Mode dispatching parallel sweeps)
- Risk of two agents picking the same issue
- Need graceful shutdown capability

**Skip atomic claiming when:**
- Single Builder in Manual Orchestration Mode
- Human directly assigns issues
- Testing/debugging workflows

### Graceful Degradation

If `loom-claim` or `signal.sh` don't exist (older installations), fall back to label-only claiming:

```bash
# Check if claiming system exists
if command -v loom-claim &>/dev/null; then
  # Use atomic claiming
  loom-claim claim "$ISSUE_NUMBER" "$AGENT_ID" || continue
fi

# Always update labels (works with or without claiming system)
gh issue edit "$ISSUE_NUMBER" --remove-label "loom:issue" --add-label "loom:building"
```
