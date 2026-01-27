# Shepherd Lifecycle Reference

This document contains detailed workflow implementation for the Shepherd role. For core role definition, principles, and phase flow overview, see `shepherd.md`.

## Graceful Shutdown - Detailed Implementation

### Checkpoint Logic

Before starting each phase, check for the shutdown signal:

```bash
# Check for graceful shutdown signal
check_shutdown_signal() {
    if [ -f .loom/stop-shepherds ]; then
        echo "Shutdown signal detected - exiting gracefully at phase boundary"

        # Revert issue label so it can be picked up again
        if [ -n "$ISSUE_NUMBER" ]; then
            LABELS=$(gh issue view $ISSUE_NUMBER --json labels --jq '.labels[].name')
            if echo "$LABELS" | grep -q "loom:building"; then
                gh issue edit $ISSUE_NUMBER --remove-label "loom:building" --add-label "loom:issue"
                gh issue comment $ISSUE_NUMBER --body "$(cat <<'EOF'
**Shepherd graceful shutdown**

Orchestration paused at phase boundary due to daemon shutdown signal.
Issue returned to `loom:issue` state for pickup when daemon restarts.

Progress preserved - next shepherd will resume from current state.
EOF
)"
            fi
        fi

        echo "Graceful exit complete"
        exit 0
    fi
}
```

### Phase Boundary Checks

Insert shutdown checks at these points in the orchestration flow:

```bash
# Example: After Builder phase completes
echo "Builder phase complete - PR #$PR_NUMBER created"
check_shutdown_signal  # Insert check here
echo "Proceeding to Judge phase..."
```

### Behavior Summary

| Signal Detected | Current State | Action |
|-----------------|---------------|--------|
| `.loom/stop-shepherds` exists | `loom:building` | Revert to `loom:issue`, exit |
| `.loom/stop-shepherds` exists | Mid-phase (building code) | Complete current phase, then check |
| No signal | Any | Continue normally |

### Why Phase Boundaries?

Checking only at phase boundaries ensures:
- **Work integrity**: Current phase completes fully (no half-built features)
- **Clean state**: Issue labels accurately reflect progress
- **Resumability**: Next shepherd can pick up from a known state
- **Responsiveness**: Shutdown happens within one phase duration (not 5+ minutes)

### Per-Issue Abort

For aborting a specific shepherd without stopping all shepherds, add `loom:abort` label to the issue:

```bash
# Also check for per-issue abort
if echo "$LABELS" | grep -q "loom:abort"; then
    echo "Abort signal detected for issue #$ISSUE_NUMBER"
    gh issue edit $ISSUE_NUMBER --remove-label "loom:abort" --remove-label "loom:building" --add-label "loom:issue"
    gh issue comment $ISSUE_NUMBER --body "**Shepherd aborted** per \`loom:abort\` label. Issue returned to \`loom:issue\` state."
    exit 0
fi
```

## Direct Mode - Detailed Examples

### Phase-Specific Task Subagent Execution

**Curator Phase (Task Subagent):**
```python
# Spawn curator subagent with fresh context
# Use sonnet - curation is structured enhancement work
result = Task(
    description=f"Curate issue #{issue_number} - add implementation details and acceptance criteria",
    prompt=f"/curator {issue_number}",
    subagent_type="general-purpose",
    model="sonnet",
    run_in_background=False
)

# Verify completion by checking labels
labels = gh_issue_view(issue_number, "--json labels --jq '.labels[].name'")
assert "loom:curated" in labels or "loom:issue" in labels
```

**Builder Phase (Task Subagent):**
```python
# Spawn builder subagent with fresh context
# Use opus - implementation requires deep reasoning
result = Task(
    description=f"Build issue #{issue_number} - implement feature and create PR",
    prompt=f"/builder {issue_number}",
    subagent_type="general-purpose",
    model="opus",
    run_in_background=False
)

# Verify completion by checking for PR
pr_number = gh_pr_list(f"--search 'Closes #{issue_number}' --json number --jq '.[0].number'")
assert pr_number is not None
```

**Judge Phase (Task Subagent):**
```python
# Spawn judge subagent with fresh context
# Use opus - thorough code review needs deep understanding
result = Task(
    description=f"Review PR #{pr_number} for issue #{issue_number}",
    prompt=f"/judge {pr_number}",
    subagent_type="general-purpose",
    model="opus",
    run_in_background=False
)

# Verify completion by checking PR labels
labels = gh_pr_view(pr_number, "--json labels --jq '.labels[].name'")
if "loom:pr" in labels:
    phase = "gate2"  # Approved
elif "loom:changes-requested" in labels:
    phase = "doctor"  # Needs fixes
```

**Doctor Phase (Task Subagent):**
```python
# Spawn doctor subagent with fresh context
# Use sonnet - PR fixes are usually targeted and scoped
result = Task(
    description=f"Address review feedback on PR #{pr_number} for issue #{issue_number}",
    prompt=f"/doctor {pr_number}",
    subagent_type="general-purpose",
    model="sonnet",
    run_in_background=False
)

# Verify completion by checking for review-requested label
labels = gh_pr_view(pr_number, "--json labels --jq '.labels[].name'")
assert "loom:review-requested" in labels
```

### Complete Direct Mode Example

Here's the full orchestration flow using Task subagents with phase-specific models:

```python
# Shepherd orchestrating issue #123 in Direct Mode
issue_number = 123

# Phase 1: Curator (sonnet - structured enhancement)
print(f"Starting Curator phase for issue #{issue_number}...")
Task(
    description=f"Curator phase for #{issue_number}",
    prompt=f"/curator {issue_number}",
    subagent_type="general-purpose",
    model="sonnet",
    run_in_background=False
)
print("Curator phase complete")

# Gate 1: Wait for approval (or auto-approve in force mode)
if force_mode:
    gh_issue_edit(issue_number, "--add-label 'loom:issue'")

# Phase 2: Builder (opus - complex implementation)
print(f"Starting Builder phase for issue #{issue_number}...")
Task(
    description=f"Builder phase for #{issue_number}",
    prompt=f"/builder {issue_number}",
    subagent_type="general-purpose",
    model="opus",
    run_in_background=False
)
pr_number = get_pr_for_issue(issue_number)
print(f"Builder phase complete - PR #{pr_number} created")

# Phase 3: Judge (opus - thorough code review)
print(f"Starting Judge phase for PR #{pr_number}...")
Task(
    description=f"Judge phase for PR #{pr_number}",
    prompt=f"/judge {pr_number}",
    subagent_type="general-purpose",
    model="opus",
    run_in_background=False
)
print("Judge phase complete")

# Continue with Doctor loop (sonnet) and merge as needed...
```

## Triggering Terminals (MCP Mode)

### Finding Terminal IDs

Before triggering, identify which terminal runs which role:

```bash
# List all terminals
mcp__loom__list_terminals

# Returns terminal IDs and their configurations
# Example output:
# terminal-1: Judge (judge.md)
# terminal-2: Curator (curator.md)
# terminal-3: Builder (builder.md)
```

### Restart for Fresh Context

Before triggering a role, restart the terminal to clear context:

```bash
# Restart terminal to clear context
mcp__loom__restart_terminal --terminal_id terminal-2
```

### Configure Phase-Specific Prompt

Set the interval prompt to focus on the specific issue:

```bash
# Configure with issue-specific prompt
mcp__loom__configure_terminal \
  --terminal_id terminal-2 \
  --interval_prompt "Curate issue #123. Follow .loom/roles/curator.md"
```

### Trigger Immediate Run

Execute the role immediately:

```bash
# Trigger immediate run
mcp__loom__trigger_run_now --terminalId terminal-2
```

### Full Trigger Sequence (MCP Mode)

For each phase, execute this sequence:

```bash
# 1. Restart for fresh context
mcp__loom__restart_terminal --terminal_id <terminal-id>

# 2. Configure with phase-specific prompt
mcp__loom__configure_terminal \
  --terminal_id <terminal-id> \
  --interval_prompt "<Role> for issue #<N>. <specific instructions>"

# 3. Trigger immediate execution
mcp__loom__trigger_run_now --terminalId <terminal-id>
```

## Waiting for Completion

**Note**: In Direct Mode, the Task subagent runs synchronously (run_in_background=False), so you know when the phase completes. However, you should still verify success by polling labels - the subagent may have encountered issues or been unable to complete its task.

### Label Polling (MCP Mode Only)

Poll labels every 30 seconds to detect phase completion:

```bash
# Poll for label changes
while true; do
  labels=$(gh issue view <number> --json labels --jq '.labels[].name')

  # Check for expected completion label
  if echo "$labels" | grep -q "loom:curated"; then
    echo "Curator phase complete"
    break
  fi

  # Check for blocked state
  if echo "$labels" | grep -q "loom:blocked"; then
    echo "Issue is blocked"
    exit 1
  fi

  sleep 30
done
```

### PR Label Polling

For PR-related phases, poll the PR instead:

```bash
# Find PR for issue
PR_NUMBER=$(gh pr list --search "Closes #<issue-number>" --json number --jq '.[0].number')

# Poll PR labels
labels=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name')

if echo "$labels" | grep -q "loom:pr"; then
  echo "PR approved, ready for merge"
elif echo "$labels" | grep -q "loom:changes-requested"; then
  echo "Changes requested, triggering Doctor"
fi
```

### Terminal Output Monitoring

Optionally check terminal output for completion signals:

```bash
output=$(mcp__loom__get_terminal_output --terminal_id terminal-2 --lines 100)

if echo "$output" | grep -q "Role Assumed: Curator"; then
  echo "Curator completed its iteration"
fi
```

## State Tracking

### Progress Comments

Track progress in issue comments for crash recovery:

```bash
# Add progress comment with hidden state
gh issue comment <number> --body "$(cat <<'EOF'
## Loom Orchestration Progress

| Phase | Status | Timestamp |
|-------|--------|-----------|
| Curator | Complete | 2025-01-23T10:00:00Z |
| Builder | In Progress | 2025-01-23T10:05:00Z |
| Judge | Pending | - |
| Doctor | Pending | - |
| Merge | Pending | - |

<!-- loom:orchestrator
{"phase":"builder","iteration":0,"pr":null,"started":"2025-01-23T10:05:00Z"}
-->
EOF
)"
```

### Resuming on Restart

When `/shepherd <number>` is invoked, check for existing progress:

```bash
# Read issue comments for existing state
STATE=$(gh issue view <number> --comments --json body \
  --jq '.comments[].body | capture("<!-- loom:orchestrator\\n(?<json>.*)\\n-->"; "m") | .json')

if [ -n "$STATE" ]; then
  PHASE=$(echo "$STATE" | jq -r '.phase')
  echo "Resuming from phase: $PHASE"
else
  echo "Starting fresh orchestration"
fi
```

## Full Orchestration Workflow

### Step 1: Check State

```bash
# Analyze issue state
LABELS=$(gh issue view <number> --json labels --jq '.labels[].name')

# Determine starting phase
# IMPORTANT: Always ensure curation happens before building
if echo "$LABELS" | grep -q "loom:building"; then
  PHASE="builder"  # Already claimed, skip to monitoring
elif echo "$LABELS" | grep -q "loom:curated"; then
  # Issue has been curated
  if echo "$LABELS" | grep -q "loom:issue"; then
    PHASE="builder"  # Curated AND approved - ready for building
  else
    PHASE="gate1"    # Curated but waiting for approval
  fi
else
  # Issue has NOT been curated - always run curator first
  # Even if loom:issue is present, curation ensures quality
  PHASE="curator"
fi
```

### Step 2: Curator Phase

```bash
if [ "$PHASE" = "curator" ]; then
  # Find curator terminal
  CURATOR_TERMINAL="terminal-2"  # or lookup from config

  # Restart and configure
  mcp__loom__restart_terminal --terminal_id $CURATOR_TERMINAL
  mcp__loom__configure_terminal \
    --terminal_id $CURATOR_TERMINAL \
    --interval_prompt "Curate issue #$ISSUE_NUMBER. Add implementation details and acceptance criteria."

  # Trigger
  mcp__loom__trigger_run_now --terminalId $CURATOR_TERMINAL

  # Wait for completion
  while true; do
    LABELS=$(gh issue view $ISSUE_NUMBER --json labels --jq '.labels[].name')
    if echo "$LABELS" | grep -q "loom:curated\|loom:issue"; then
      break
    fi
    sleep 30
  done

  # Update progress
  update_progress "curator" "complete"
fi
```

### Step 3: Gate 1 - Approval

```bash
if [ "$PHASE" = "gate1" ]; then
  # Check if --force-pr or --force-merge mode - auto-approve
  if [ "$FORCE_PR" = "true" ] || [ "$FORCE_MERGE" = "true" ]; then
    echo "Force mode: auto-approving issue"
    gh issue edit $ISSUE_NUMBER --add-label "loom:issue"
    gh issue comment $ISSUE_NUMBER --body "**Auto-approved** via \`/shepherd --force-pr\` or \`--force-merge\`"
  else
    # Wait for human or Champion to promote to loom:issue
    TIMEOUT=1800  # 30 minutes
    START=$(date +%s)

    while true; do
      LABELS=$(gh issue view $ISSUE_NUMBER --json labels --jq '.labels[].name')
      if echo "$LABELS" | grep -q "loom:issue"; then
        echo "Issue approved for implementation"
        break
      fi

      NOW=$(date +%s)
      if [ $((NOW - START)) -gt $TIMEOUT ]; then
        echo "Timeout waiting for approval"
        gh issue comment $ISSUE_NUMBER --body "Orchestration paused: waiting for approval (loom:issue label)"
        exit 0
      fi

      sleep 30
    done
  fi
fi
```

### Step 4: Builder Phase

```bash
if [ "$PHASE" = "builder" ]; then
  BUILDER_TERMINAL="terminal-3"

  mcp__loom__restart_terminal --terminal_id $BUILDER_TERMINAL
  mcp__loom__configure_terminal \
    --terminal_id $BUILDER_TERMINAL \
    --interval_prompt "Build issue #$ISSUE_NUMBER. Create worktree, implement, test, create PR."

  mcp__loom__trigger_run_now --terminalId $BUILDER_TERMINAL

  # Wait for PR creation
  while true; do
    # Check if a PR exists for this issue
    PR_NUMBER=$(gh pr list --search "Closes #$ISSUE_NUMBER" --state open --json number --jq '.[0].number')
    if [ -n "$PR_NUMBER" ]; then
      echo "PR #$PR_NUMBER created"
      break
    fi
    sleep 30
  done

  update_progress "builder" "complete" "$PR_NUMBER"
fi
```

### Step 5: Judge Phase

```bash
if [ "$PHASE" = "judge" ]; then
  JUDGE_TERMINAL="terminal-1"

  mcp__loom__restart_terminal --terminal_id $JUDGE_TERMINAL
  mcp__loom__configure_terminal \
    --terminal_id $JUDGE_TERMINAL \
    --interval_prompt "Review PR #$PR_NUMBER for issue #$ISSUE_NUMBER."

  mcp__loom__trigger_run_now --terminalId $JUDGE_TERMINAL

  # Wait for review completion
  # Note: Judge uses label-based reviews (comment + label change), not GitHub's
  # review API, so self-approval is not a problem. See judge.md for details.
  while true; do
    LABELS=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name')
    if echo "$LABELS" | grep -q "loom:pr"; then
      echo "PR approved"
      PHASE="gate2"
      break
    elif echo "$LABELS" | grep -q "loom:changes-requested"; then
      echo "Changes requested"
      PHASE="doctor"
      break
    fi
    sleep 30
  done
fi
```

### Step 6: Doctor Loop

```bash
MAX_DOCTOR_ITERATIONS=3
DOCTOR_ITERATION=0

while [ "$PHASE" = "doctor" ] && [ $DOCTOR_ITERATION -lt $MAX_DOCTOR_ITERATIONS ]; do
  DOCTOR_TERMINAL="terminal-4"  # or lookup

  mcp__loom__restart_terminal --terminal_id $DOCTOR_TERMINAL
  mcp__loom__configure_terminal \
    --terminal_id $DOCTOR_TERMINAL \
    --interval_prompt "Address review feedback on PR #$PR_NUMBER for issue #$ISSUE_NUMBER."

  mcp__loom__trigger_run_now --terminalId $DOCTOR_TERMINAL

  # Wait for Doctor to complete and re-trigger Judge
  while true; do
    LABELS=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name')
    if echo "$LABELS" | grep -q "loom:review-requested"; then
      echo "Doctor completed, returning to Judge"
      PHASE="judge"
      break
    fi
    sleep 30
  done

  DOCTOR_ITERATION=$((DOCTOR_ITERATION + 1))

  # If we've returned to judge phase, run the judge again
  if [ "$PHASE" = "judge" ]; then
    # ... trigger judge again (same as Step 5) ...

    # Check result
    LABELS=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name')
    if echo "$LABELS" | grep -q "loom:pr"; then
      PHASE="gate2"
      break
    elif echo "$LABELS" | grep -q "loom:changes-requested"; then
      PHASE="doctor"
      # Continue loop
    fi
  fi
done

if [ $DOCTOR_ITERATION -ge $MAX_DOCTOR_ITERATIONS ]; then
  gh issue comment $ISSUE_NUMBER --body "**Orchestration blocked**: Maximum Doctor iterations ($MAX_DOCTOR_ITERATIONS) reached without approval. Manual intervention required."
  gh issue edit $ISSUE_NUMBER --add-label "loom:blocked"
  exit 1
fi
```

### Step 7: Gate 2 - Merge

```bash
if [ "$PHASE" = "gate2" ]; then
  # Check if --force-pr mode - stop here, don't merge
  if [ "$FORCE_PR" = "true" ]; then
    echo "Force-pr mode: stopping at loom:pr state"
    gh issue comment $ISSUE_NUMBER --body "**PR approved** - stopping at \`loom:pr\` per \`--force-pr\`. Ready for human merge."
    exit 0
  fi

  # Check if --force-merge mode - auto-merge with conflict resolution
  if [ "$FORCE_MERGE" = "true" ]; then
    echo "Force-merge mode: auto-merging PR"

    # IMPORTANT: Worktree Checkout Error Handling
    # ============================================
    # When running from a worktree, `gh pr merge` may succeed on GitHub but fail
    # locally with: "fatal: 'main' is already used by worktree at '/path/to/repo'"
    #
    # This is EXPECTED behavior - the merge completes remotely but git can't switch
    # to main locally because another worktree already has it checked out.
    #
    # Solution: Always verify PR state via GitHub API rather than relying on exit code.
    # The exit code of `gh pr merge` is unreliable when running from worktrees.

    MERGE_OUTPUT=$(gh pr merge $PR_NUMBER --squash --delete-branch 2>&1)
    MERGE_EXIT=$?

    # Always verify actual merge state via GitHub API (exit code is unreliable in worktrees)
    PR_STATE=$(gh pr view $PR_NUMBER --json state --jq '.state')

    if [ "$PR_STATE" = "MERGED" ]; then
      # Merge succeeded - any error was just the local checkout failure (expected in worktrees)
      if [ $MERGE_EXIT -ne 0 ]; then
        if echo "$MERGE_OUTPUT" | grep -q "already used by worktree"; then
          echo "PR merged successfully (local checkout skipped - worktree conflict is expected)"
        else
          echo "PR merged successfully (non-fatal local error ignored)"
        fi
      else
        echo "PR merged successfully"
      fi
    else
      # Merge actually failed on GitHub - this is a real error that needs handling
      echo "Merge failed (PR state: $PR_STATE)"

      # Check for merge conflicts
      MERGEABLE=$(gh pr view $PR_NUMBER --json mergeable --jq '.mergeable')
      if [ "$MERGEABLE" = "CONFLICTING" ]; then
        echo "Attempting conflict resolution..."
        git fetch origin main
        git checkout $BRANCH_NAME 2>/dev/null || git checkout -b $BRANCH_NAME origin/$BRANCH_NAME
        git merge origin/main --no-edit || {
          # Auto-resolve conflicts if possible
          git checkout --theirs .
          git add -A
          git commit -m "Resolve merge conflicts (auto-resolved)"
        }
        git push origin $BRANCH_NAME

        # Retry merge and verify via API (not exit code)
        gh pr merge $PR_NUMBER --squash --delete-branch 2>&1
        PR_STATE=$(gh pr view $PR_NUMBER --json state --jq '.state')
        if [ "$PR_STATE" = "MERGED" ]; then
          echo "PR merged successfully after conflict resolution"
        else
          echo "Merge failed after conflict resolution"
          exit 1
        fi
      else
        echo "Merge failed: $MERGE_OUTPUT"
        exit 1
      fi
    fi

    gh issue comment $ISSUE_NUMBER --body "**Auto-merged** PR #$PR_NUMBER via \`/shepherd --force-merge\`"
  else
    # Trigger Champion or wait for human merge
    CHAMPION_TERMINAL="terminal-5"  # if exists

    mcp__loom__trigger_run_now --terminalId $CHAMPION_TERMINAL

    # Wait for merge
    TIMEOUT=1800  # 30 minutes
    START=$(date +%s)

    while true; do
      PR_STATE=$(gh pr view $PR_NUMBER --json state --jq '.state')
      if [ "$PR_STATE" = "MERGED" ]; then
        echo "PR merged successfully"
        break
      elif [ "$PR_STATE" = "CLOSED" ]; then
        echo "PR was closed without merging"
        exit 1
      fi

      NOW=$(date +%s)
      if [ $((NOW - START)) -gt $TIMEOUT ]; then
        echo "Timeout waiting for merge"
        gh issue comment $ISSUE_NUMBER --body "Orchestration complete: PR #$PR_NUMBER is approved and ready for merge."
        exit 0
      fi

      sleep 30
    done
  fi
fi
```

### Step 8: Complete

```bash
# Final status report
gh issue comment $ISSUE_NUMBER --body "$(cat <<EOF
## Orchestration Complete

Issue #$ISSUE_NUMBER has been successfully shepherded through the development lifecycle:

| Phase | Status |
|-------|--------|
| Curator | Enhanced with implementation details |
| Approval | Approved for implementation |
| Builder | Implemented in PR #$PR_NUMBER |
| Judge | Code review passed |
| Merge | PR merged |

**Total orchestration time**: $DURATION

<!-- loom:orchestrator
{"phase":"complete","pr":$PR_NUMBER,"completed":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
-->
EOF
)"
```

## Terminal Configuration Requirements (MCP Mode Only)

For MCP Mode orchestration, you need these terminals configured:

| Terminal | Role | Suggested Name |
|----------|------|----------------|
| terminal-1 | judge.md | Judge |
| terminal-2 | curator.md | Curator |
| terminal-3 | builder.md | Builder |
| terminal-4 | doctor.md | Doctor |
| terminal-5 | champion.md | Champion (optional) |

You can discover terminal configurations with:

```bash
mcp__loom__get_ui_state
```

**Note**: In Direct Mode, terminal configuration is not required. The orchestrator spawns Task subagents for each role phase.

## Auto-Configuring Missing Terminals (Force Mode)

In MCP Mode, when `--force`, `--force-pr`, or `--force-merge` is specified, the orchestrator automatically configures any missing required terminals instead of prompting the user.

### Why Auto-Configure?

Force mode implies minimal user interaction. Stopping to ask "Add Builder terminal?" defeats the purpose. The orchestrator should:
1. Detect missing terminals
2. Auto-configure with sensible defaults
3. Log what was configured
4. Continue orchestration

### Detection Logic

Before each phase, check if the required terminal exists:

```bash
# Check for a terminal with specific role
TERMINALS=$(mcp__loom__list_terminals)
BUILDER_TERMINAL=$(echo "$TERMINALS" | jq -r '.[] | select(.roleConfig.roleFile == "builder.md") | .id' | head -1)

if [ -z "$BUILDER_TERMINAL" ]; then
  if [ "$FORCE_MODE" = "true" ]; then
    # Auto-configure the missing terminal
    auto_configure_terminal "builder"
  else
    # Prompt user (normal mode behavior)
    echo "Missing Builder terminal. Add one?"
  fi
fi
```

### Auto-Configuration Process

When a required terminal is missing and force mode is active:

**Step 1: Read Role Defaults**

```bash
# Load defaults from role JSON file
ROLE_JSON=$(cat .loom/roles/builder.json)
ROLE_NAME=$(echo "$ROLE_JSON" | jq -r '.name')                    # "Development Worker"
WORKER_TYPE=$(echo "$ROLE_JSON" | jq -r '.suggestedWorkerType')   # "claude"
INTERVAL=$(echo "$ROLE_JSON" | jq -r '.defaultInterval')          # 0
```

**Step 2: Create Terminal via MCP**

```bash
# Create the terminal with role defaults
mcp__loom__create_terminal \
  --name "$ROLE_NAME" \
  --role "builder"
```

**Step 3: Configure Role Settings**

```bash
# Get the new terminal ID (will be terminal-N based on nextAgentNumber)
NEW_TERMINAL_ID=$(mcp__loom__list_terminals | jq -r '.[-1].id')

# Configure with role-specific settings
mcp__loom__configure_terminal \
  --terminal_id "$NEW_TERMINAL_ID" \
  --target_interval "$INTERVAL" \
  --role_file "builder.md"
```

**Step 4: Log What Was Configured**

```bash
echo "Auto-configured $ROLE_NAME terminal ($NEW_TERMINAL_ID)"
```

### Role Defaults Reference

Each role has defaults in its JSON metadata file:

| Role | Name | Worker Type | Interval | Autonomous |
|------|------|-------------|----------|------------|
| builder | Development Worker | claude | 0 | No |
| curator | Issue Curator | codex | 300000 | Yes |
| judge | Code Review Specialist | codex | 300000 | Yes |
| doctor | PR Fixer | claude | 300000 | Yes |
| champion | PR Champion | codex | 600000 | Yes |

### Terminal Configuration Structure

Auto-configured terminals follow this structure:

```json
{
  "id": "terminal-N",
  "name": "<role.name from JSON>",
  "role": "<role-key>",
  "roleConfig": {
    "workerType": "<role.suggestedWorkerType>",
    "roleFile": "<role>.md",
    "targetInterval": "<role.defaultInterval>",
    "intervalPrompt": ""
  }
}
```

### Complete Auto-Configuration Function

```bash
auto_configure_terminal() {
  local ROLE_KEY=$1  # e.g., "builder", "curator", "judge"

  # Read role metadata
  local ROLE_JSON_FILE=".loom/roles/${ROLE_KEY}.json"
  if [ ! -f "$ROLE_JSON_FILE" ]; then
    echo "ERROR: Role file not found: $ROLE_JSON_FILE"
    return 1
  fi

  local ROLE_JSON=$(cat "$ROLE_JSON_FILE")
  local ROLE_NAME=$(echo "$ROLE_JSON" | jq -r '.name // "Unknown Role"')
  local WORKER_TYPE=$(echo "$ROLE_JSON" | jq -r '.suggestedWorkerType // "claude"')
  local INTERVAL=$(echo "$ROLE_JSON" | jq -r '.defaultInterval // 0')

  # Create terminal
  mcp__loom__create_terminal \
    --name "$ROLE_NAME" \
    --role "$ROLE_KEY"

  # Get newly created terminal ID
  local NEW_TERMINAL_ID=$(mcp__loom__list_terminals | jq -r '.[-1].id')

  # Configure role settings
  mcp__loom__configure_terminal \
    --terminal_id "$NEW_TERMINAL_ID" \
    --target_interval "$INTERVAL" \
    --role_file "${ROLE_KEY}.md"

  echo "Auto-configured $ROLE_NAME terminal ($NEW_TERMINAL_ID)"

  # Return the terminal ID for use
  echo "$NEW_TERMINAL_ID"
}
```

### Usage in Phase Execution

Before triggering each phase, check and auto-configure:

```bash
# Example: Builder phase with auto-configuration
if [ "$PHASE" = "builder" ]; then
  # Find existing Builder terminal
  BUILDER_TERMINAL=$(mcp__loom__list_terminals | \
    jq -r '.[] | select(.roleConfig.roleFile == "builder.md") | .id' | head -1)

  # Auto-configure if missing and in force mode
  if [ -z "$BUILDER_TERMINAL" ]; then
    if [ "$FORCE_MODE" = "true" ]; then
      BUILDER_TERMINAL=$(auto_configure_terminal "builder")
    else
      echo "ERROR: No Builder terminal configured"
      exit 1
    fi
  fi

  # Now proceed with the phase using $BUILDER_TERMINAL
  mcp__loom__restart_terminal --terminal_id "$BUILDER_TERMINAL"
  mcp__loom__configure_terminal \
    --terminal_id "$BUILDER_TERMINAL" \
    --interval_prompt "Build issue #$ISSUE_NUMBER"
  mcp__loom__trigger_run_now --terminalId "$BUILDER_TERMINAL"
fi
```

### Behavior Summary

| Mode | Missing Terminal | Behavior |
|------|------------------|----------|
| Normal (`/shepherd N`) | Builder missing | Prompt user: "Add Builder terminal?" |
| Force (`--force`) | Builder missing | Auto-configure Builder, log, continue |
| Force PR (`--force-pr`) | Builder missing | Auto-configure Builder, log, continue |
| Force Merge (`--force-merge`) | Builder missing | Auto-configure Builder, log, continue |
| Direct Mode | Any missing | N/A - executes roles directly |

### Persistence

Auto-configured terminals are persisted to `.loom/config.json` by the MCP server. They will be available for future orchestrations.

## Error Handling Details

### Terminal Not Found

If a required terminal isn't configured:

**In Force Mode** (`--force`, `--force-pr`, `--force-merge`):
- Auto-configure the terminal using defaults from `.loom/roles/<role>.json`
- See "Auto-Configuring Missing Terminals" section above

**In Normal Mode**:
```bash
# Prompt user for action
echo "Missing $ROLE terminal. Options:"
echo "1. Add $ROLE terminal with default configuration"
echo "2. Skip this phase (may cause issues)"
echo "3. Abort orchestration"

# If user chooses to abort:
echo "ERROR: No terminal found for role '$ROLE'. Configure a terminal with roleFile: $ROLE.md"
gh issue comment $ISSUE_NUMBER --body "**Orchestration paused**: Missing terminal for $ROLE role. Run with --force to auto-configure."
```
