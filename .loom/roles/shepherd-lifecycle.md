# Shepherd Lifecycle Reference

This document contains detailed workflow implementation for the Shepherd role. For core role definition, principles, and phase flow overview, see `shepherd.md`.

## Graceful Shutdown - Integrated into Waits

Shutdown signal checking is integrated into `agent-wait-bg.sh`, which polls for signals during phase waits. This replaces the previous approach of checking only at phase boundaries.

### How It Works

`agent-wait-bg.sh` runs `agent-wait.sh` in the background and polls for shutdown signals every poll interval. When a signal is detected, it kills the background wait, and returns exit code 3.

### Signals Checked

| Signal | Scope | Detection |
|--------|-------|-----------|
| `.loom/stop-shepherds` file | All shepherds | File existence check |
| `loom:abort` label | Single issue | GitHub label check (requires `--issue`) |

### Handling Exit Code 3

When `agent-wait-bg.sh` returns exit code 3 (detected via polling), the shepherd should clean up and exit:

```bash
# Run wait in background
Bash(command="./.loom/scripts/agent-wait-bg.sh '${ROLE}-issue-${ISSUE_NUMBER}' --timeout 900 --issue '$ISSUE_NUMBER'", run_in_background=true)
# Returns WAIT_TASK_ID

# Poll loop with heartbeat reporting
while not completed:
    result = TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
    if result.status == "completed":
        WAIT_EXIT = result.exit_code
        break
    ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for ${ROLE}"
    sleep 15

if [ "$WAIT_EXIT" -eq 3 ]; then
    echo "Shutdown signal detected during ${ROLE} phase"
    ./.loom/scripts/agent-destroy.sh "${ROLE}-issue-${ISSUE_NUMBER}"

    # Revert issue label so it can be picked up again
    LABELS=$(gh issue view $ISSUE_NUMBER --json labels --jq '.labels[].name')
    if echo "$LABELS" | grep -q "loom:building"; then
        gh issue edit $ISSUE_NUMBER --remove-label "loom:building" --add-label "loom:issue"
    fi
    if echo "$LABELS" | grep -q "loom:abort"; then
        gh issue edit $ISSUE_NUMBER --remove-label "loom:abort"
        gh issue comment $ISSUE_NUMBER --body "**Shepherd aborted** per \`loom:abort\` label. Issue returned to \`loom:issue\` state."
    else
        gh issue comment $ISSUE_NUMBER --body "**Shepherd graceful shutdown** - orchestration paused during ${ROLE} phase. Issue returned to \`loom:issue\` state."
    fi
    exit 0
fi
```

### Behavior Summary

| Signal Detected | When | Action |
|-----------------|------|--------|
| `.loom/stop-shepherds` | During wait | Kill worker wait, clean up, revert labels, exit |
| `loom:abort` label | During wait | Kill worker wait, clean up, revert labels, exit |
| No signal | Wait completes | Continue to label verification |

## tmux Worker Execution - Detailed Examples

### Phase-Specific Worker Execution

**Curator Phase:**
```bash
# Spawn curator worker in ephemeral tmux session
./.loom/scripts/agent-spawn.sh --role curator --name "curator-issue-${ISSUE}" --args "$ISSUE" --on-demand

# Non-blocking wait with heartbeat polling
Bash(command="./.loom/scripts/agent-wait-bg.sh 'curator-issue-${ISSUE}' --timeout 600 --issue '$ISSUE'", run_in_background=true)
# Poll loop: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
# Each iteration: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for curator"
# When completed: WAIT_EXIT = result.exit_code
[ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "curator-issue-${ISSUE}"; handle_shutdown; }

# Clean up
./.loom/scripts/agent-destroy.sh "curator-issue-${ISSUE}"

# Validate phase contract (recovers by applying loom:curated if missing)
./.loom/scripts/validate-phase.sh curator "$ISSUE" --task-id "$TASK_ID"
[ $? -ne 0 ] && exit 1
```

**Builder Phase:**
```bash
# Spawn builder worker with worktree isolation
./.loom/scripts/agent-spawn.sh --role builder --name "builder-issue-${ISSUE}" --args "$ISSUE" \
    --worktree ".loom/worktrees/issue-${ISSUE}" --on-demand

# Non-blocking wait with heartbeat polling
Bash(command="./.loom/scripts/agent-wait-bg.sh 'builder-issue-${ISSUE}' --timeout 1800 --issue '$ISSUE'", run_in_background=true)
# Poll loop: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
# Each iteration: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for builder"
# When completed: WAIT_EXIT = result.exit_code
[ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "builder-issue-${ISSUE}"; handle_shutdown; }

# Clean up (worktree stays for judge/doctor phases)
./.loom/scripts/agent-destroy.sh "builder-issue-${ISSUE}"

# Validate phase contract (recovers by committing/pushing worktree and creating PR)
./.loom/scripts/validate-phase.sh builder "$ISSUE" --worktree ".loom/worktrees/issue-${ISSUE}" --task-id "$TASK_ID"
[ $? -ne 0 ] && exit 1

# Get PR number for subsequent phases
PR_NUMBER=$(gh pr list --search "Closes #${ISSUE}" --state open --json number --jq '.[0].number')
```

**Judge Phase:**
```bash
# Spawn judge worker
./.loom/scripts/agent-spawn.sh --role judge --name "judge-issue-${ISSUE}" --args "$PR_NUMBER" --on-demand

# Non-blocking wait with heartbeat polling
Bash(command="./.loom/scripts/agent-wait-bg.sh 'judge-issue-${ISSUE}' --timeout 900 --issue '$ISSUE'", run_in_background=true)
# Poll loop: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
# Each iteration: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for judge"
# When completed: WAIT_EXIT = result.exit_code
[ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE}"; handle_shutdown; }

# Clean up
./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE}"

# Validate phase contract (no recovery — marks loom:blocked if neither label present)
./.loom/scripts/validate-phase.sh judge "$ISSUE" --pr "$PR_NUMBER" --task-id "$TASK_ID"
[ $? -ne 0 ] && exit 1

# Determine next phase from PR labels
LABELS=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name')
if echo "$LABELS" | grep -q "loom:pr"; then
    PHASE="gate2"  # Approved
elif echo "$LABELS" | grep -q "loom:changes-requested"; then
    PHASE="doctor"  # Needs fixes
fi
```

**Doctor Phase:**
```bash
# Spawn doctor worker
./.loom/scripts/agent-spawn.sh --role doctor --name "doctor-issue-${ISSUE}" --args "$PR_NUMBER" --on-demand

# Non-blocking wait with heartbeat polling
Bash(command="./.loom/scripts/agent-wait-bg.sh 'doctor-issue-${ISSUE}' --timeout 900 --issue '$ISSUE'", run_in_background=true)
# Poll loop: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
# Each iteration: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for doctor"
# When completed: WAIT_EXIT = result.exit_code
[ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "doctor-issue-${ISSUE}"; handle_shutdown; }

# Clean up
./.loom/scripts/agent-destroy.sh "doctor-issue-${ISSUE}"

# Validate phase contract (no recovery — marks loom:blocked if label missing)
./.loom/scripts/validate-phase.sh doctor "$ISSUE" --pr "$PR_NUMBER" --task-id "$TASK_ID"
[ $? -ne 0 ] && exit 1
```

### Complete Orchestration Example

```bash
# Shepherd orchestrating issue #123
ISSUE=123

# Helper for shutdown signal handling
handle_shutdown() {
    LABELS=$(gh issue view $ISSUE --json labels --jq '.labels[].name')
    if echo "$LABELS" | grep -q "loom:building"; then
        gh issue edit $ISSUE --remove-label "loom:building" --add-label "loom:issue"
    fi
    echo "Shutdown signal detected - exiting gracefully"
    exit 0
}

# Helper: non-blocking wait with heartbeat polling
# Usage: wait_with_heartbeat <session-name> <timeout> <role-label>
# Sets WAIT_EXIT when complete
wait_with_heartbeat() {
    local SESSION_NAME="$1" TIMEOUT="$2" ROLE_LABEL="$3"
    # Run wait in background
    Bash(command="./.loom/scripts/agent-wait-bg.sh '${SESSION_NAME}' --timeout ${TIMEOUT} --issue '$ISSUE'", run_in_background=true)
    # Returns WAIT_TASK_ID
    # Poll loop with heartbeat
    while not completed:
        result = TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
        if result.status == "completed":
            WAIT_EXIT = result.exit_code
            break
        ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for ${ROLE_LABEL}"
        sleep 15
}

# Phase 1: Curator
echo "Starting Curator phase for issue #${ISSUE}..."
./.loom/scripts/agent-spawn.sh --role curator --name "curator-issue-${ISSUE}" --args "$ISSUE" --on-demand
wait_with_heartbeat "curator-issue-${ISSUE}" 600 "curator"
[ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "curator-issue-${ISSUE}"; handle_shutdown; }
./.loom/scripts/agent-destroy.sh "curator-issue-${ISSUE}"
./.loom/scripts/validate-phase.sh curator "$ISSUE"
[ $? -ne 0 ] && { echo "Curator phase contract failed"; exit 1; }
echo "Curator phase complete"

# Gate 1: Wait for approval (or auto-approve in force mode)
if [ "$FORCE_MODE" = "true" ]; then
    gh issue edit $ISSUE --add-label "loom:issue"
fi

# Phase 2: Builder
echo "Starting Builder phase for issue #${ISSUE}..."
./.loom/scripts/agent-spawn.sh --role builder --name "builder-issue-${ISSUE}" --args "$ISSUE" --on-demand
wait_with_heartbeat "builder-issue-${ISSUE}" 1800 "builder"
[ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "builder-issue-${ISSUE}"; handle_shutdown; }
./.loom/scripts/agent-destroy.sh "builder-issue-${ISSUE}"
./.loom/scripts/validate-phase.sh builder "$ISSUE" --worktree ".loom/worktrees/issue-${ISSUE}"
[ $? -ne 0 ] && { echo "Builder phase contract failed"; exit 1; }
PR_NUMBER=$(gh pr list --search "Closes #${ISSUE}" --json number --jq '.[0].number')
echo "Builder phase complete - PR #${PR_NUMBER} created"

# Phase 3: Judge
echo "Starting Judge phase for PR #${PR_NUMBER}..."
./.loom/scripts/agent-spawn.sh --role judge --name "judge-issue-${ISSUE}" --args "$PR_NUMBER" --on-demand
wait_with_heartbeat "judge-issue-${ISSUE}" 900 "judge"
[ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE}"; handle_shutdown; }
./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE}"
./.loom/scripts/validate-phase.sh judge "$ISSUE" --pr "$PR_NUMBER"
[ $? -ne 0 ] && { echo "Judge phase contract failed"; exit 1; }
echo "Judge phase complete"

# Continue with Doctor loop and merge as needed...
```

### Observability

All worker sessions are attachable for live observation:
```bash
# Watch builder working on issue 42
tmux -L loom attach -t loom-builder-issue-42

# List all active worker sessions
tmux -L loom list-sessions
```

## Waiting for Completion

After spawning a worker with `agent-spawn.sh`, run `agent-wait-bg.sh` in the background using `Bash(run_in_background=true)`, then poll with `TaskOutput(block=false)` in a loop. This non-blocking pattern allows the shepherd to report heartbeat milestones during the wait, keeping the daemon informed of progress.

```
# Launch wait in background
Bash(command="./.loom/scripts/agent-wait-bg.sh '<name>' --timeout <T> --issue '$ISSUE'", run_in_background=true)
# Returns WAIT_TASK_ID

# Poll with heartbeat reporting every 15 seconds
while not completed:
    result = TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
    if result.status == "completed":
        WAIT_EXIT = result.exit_code
        break
    ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for <role>"
    sleep 15
```

Exit codes from `agent-wait-bg.sh`:
- **0**: Agent completed normally
- **1**: Timeout reached
- **2**: Session not found
- **3**: Shutdown signal detected (clean up and exit)

After `agent-wait-bg.sh` returns with exit code 0, always verify success by checking labels — the worker may have encountered issues:

### Post-Phase Contract Validation

After each phase completes, use `validate-phase.sh` to verify the expected outcome occurred and attempt recovery if it didn't. This replaces manual label checking with a standardized validation + recovery pipeline.

```bash
# After each phase: spawn -> wait -> destroy -> validate
./.loom/scripts/validate-phase.sh <phase> $ISSUE [--worktree <path>] [--pr <number>] [--task-id <id>]
VALIDATE_EXIT=$?
if [ "$VALIDATE_EXIT" -ne 0 ]; then
    echo "Phase contract failed for <phase>, issue blocked"
    # validate-phase.sh already applied loom:blocked and commented
    exit 1
fi
```

**Phase contracts and recovery**:

| Phase | Expected Outcome | Recovery |
|-------|-----------------|----------|
| `curator` | `loom:curated` label on issue | Apply label (curator may have enhanced but not labeled) |
| `builder` | PR exists with `loom:review-requested` | Commit/push worktree changes, create PR |
| `judge` | `loom:pr` or `loom:changes-requested` on PR | No recovery — mark `loom:blocked` |
| `doctor` | `loom:review-requested` on PR | No recovery — mark `loom:blocked` |

Use `--json` for machine-readable output. Exit code 0 means contract satisfied (initially or after recovery), 1 means failed.

### Common Mistakes to Avoid

> **⚠️ WARNING**: These mistakes can leave issues in inconsistent states, breaking the orchestration pipeline.

| Mistake | Consequence | Prevention |
|---------|-------------|------------|
| Skipping `validate-phase.sh` | Issue may be missing labels or PR; next phase fails or gets stuck | Always validate after every phase |
| Only checking labels manually | Misses recovery opportunities; inconsistent error handling | Use `validate-phase.sh` instead of manual checks |
| Ignoring validation exit code | Issue proceeds despite failed contracts; downstream chaos | Always check `$?` and exit on failure |
| Validating before `agent-destroy.sh` | Worker session may still be writing; race conditions | Destroy session first, then validate |

**Correct order for every phase:**
```bash
# 1. Spawn worker
agent-spawn.sh ...

# 2. Wait for completion
agent-wait-bg.sh ... (in background, poll with heartbeat)

# 3. Destroy session FIRST
agent-destroy.sh ...

# 4. THEN validate (REQUIRED)
validate-phase.sh ...
[ $? -ne 0 ] && exit 1
```

### Label Verification (Legacy)

The `validate-phase.sh` script supersedes manual label checking. For reference, the previous approach:

```bash
# After curator phase
LABELS=$(gh issue view $ISSUE --json labels --jq '.labels[].name')
if echo "$LABELS" | grep -q "loom:curated"; then
  echo "Curator phase complete"
elif echo "$LABELS" | grep -q "loom:blocked"; then
  echo "Issue is blocked"
  exit 1
fi
```

### PR Label Verification (Legacy)

For PR-related phases:

```bash
# Find PR for issue
PR_NUMBER=$(gh pr list --search "Closes #$ISSUE" --json number --jq '.[0].number')

# Check PR labels
LABELS=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name')
if echo "$LABELS" | grep -q "loom:pr"; then
  echo "PR approved, ready for merge"
elif echo "$LABELS" | grep -q "loom:changes-requested"; then
  echo "Changes requested, triggering Doctor"
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
  # Spawn ephemeral curator worker
  ./.loom/scripts/agent-spawn.sh --role curator --name "curator-issue-${ISSUE_NUMBER}" --args "$ISSUE_NUMBER" --on-demand

  # Non-blocking wait with heartbeat polling
  Bash(command="./.loom/scripts/agent-wait-bg.sh 'curator-issue-${ISSUE_NUMBER}' --timeout 600 --issue '$ISSUE_NUMBER'", run_in_background=true)
  # Poll: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
  # Heartbeat: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for curator"
  # When completed: WAIT_EXIT = result.exit_code
  [ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "curator-issue-${ISSUE_NUMBER}"; handle_shutdown; }
  ./.loom/scripts/agent-destroy.sh "curator-issue-${ISSUE_NUMBER}"

  # Validate phase contract
  ./.loom/scripts/validate-phase.sh curator "$ISSUE_NUMBER" --task-id "$TASK_ID"
  if [ $? -ne 0 ]; then
    echo "Curator phase contract failed"
    exit 1
  fi

  # Update progress
  update_progress "curator" "complete"
fi
```

### Step 3: Gate 1 - Approval

```bash
if [ "$PHASE" = "gate1" ]; then
  # Check if --force or default mode - auto-approve
  if [ "$FORCE_MODE" = "true" ] || [ "$DEFAULT_MODE" = "true" ]; then
    echo "Auto-approving issue (force/default mode)"
    gh issue edit $ISSUE_NUMBER --add-label "loom:issue"
    gh issue comment $ISSUE_NUMBER --body "**Auto-approved** via shepherd orchestration"
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
  # Spawn ephemeral builder worker
  ./.loom/scripts/agent-spawn.sh --role builder --name "builder-issue-${ISSUE_NUMBER}" --args "$ISSUE_NUMBER" --on-demand

  # Non-blocking wait with heartbeat polling
  Bash(command="./.loom/scripts/agent-wait-bg.sh 'builder-issue-${ISSUE_NUMBER}' --timeout 1800 --issue '$ISSUE_NUMBER'", run_in_background=true)
  # Poll: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
  # Heartbeat: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for builder"
  # When completed: WAIT_EXIT = result.exit_code
  [ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "builder-issue-${ISSUE_NUMBER}"; handle_shutdown; }
  ./.loom/scripts/agent-destroy.sh "builder-issue-${ISSUE_NUMBER}"

  # Validate phase contract (attempts recovery from worktree if no PR)
  ./.loom/scripts/validate-phase.sh builder "$ISSUE_NUMBER" --worktree ".loom/worktrees/issue-${ISSUE_NUMBER}" --task-id "$TASK_ID"
  if [ $? -ne 0 ]; then
    echo "Builder phase contract failed"
    exit 1
  fi

  # Find the PR
  PR_NUMBER=$(gh pr list --search "Closes #$ISSUE_NUMBER" --state open --json number --jq '.[0].number')
  echo "PR #$PR_NUMBER created"

  update_progress "builder" "complete" "$PR_NUMBER"
fi
```

### Step 5: Judge Phase

```bash
if [ "$PHASE" = "judge" ]; then
  # Spawn ephemeral judge worker
  ./.loom/scripts/agent-spawn.sh --role judge --name "judge-issue-${ISSUE_NUMBER}" --args "$PR_NUMBER" --on-demand

  # Non-blocking wait with heartbeat polling
  Bash(command="./.loom/scripts/agent-wait-bg.sh 'judge-issue-${ISSUE_NUMBER}' --timeout 900 --issue '$ISSUE_NUMBER'", run_in_background=true)
  # Poll: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
  # Heartbeat: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for judge"
  # When completed: WAIT_EXIT = result.exit_code
  [ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE_NUMBER}"; handle_shutdown; }
  ./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE_NUMBER}"

  # Validate phase contract
  ./.loom/scripts/validate-phase.sh judge "$ISSUE_NUMBER" --pr "$PR_NUMBER" --task-id "$TASK_ID"
  if [ $? -ne 0 ]; then
    echo "Judge phase contract failed"
    exit 1
  fi

  # Check review result
  # Note: Judge uses label-based reviews (comment + label change), not GitHub's
  # review API, so self-approval is not a problem. See judge.md for details.
  LABELS=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name')
  if echo "$LABELS" | grep -q "loom:pr"; then
    echo "PR approved"
    PHASE="gate2"
  elif echo "$LABELS" | grep -q "loom:changes-requested"; then
    echo "Changes requested"
    PHASE="doctor"
  fi
fi
```

### Step 6: Doctor Loop

```bash
MAX_DOCTOR_ITERATIONS=3
DOCTOR_ITERATION=0

while [ "$PHASE" = "doctor" ] && [ $DOCTOR_ITERATION -lt $MAX_DOCTOR_ITERATIONS ]; do
  # Spawn ephemeral doctor worker
  ./.loom/scripts/agent-spawn.sh --role doctor --name "doctor-issue-${ISSUE_NUMBER}" --args "$PR_NUMBER" --on-demand

  # Non-blocking wait with heartbeat polling
  Bash(command="./.loom/scripts/agent-wait-bg.sh 'doctor-issue-${ISSUE_NUMBER}' --timeout 900 --issue '$ISSUE_NUMBER'", run_in_background=true)
  # Poll: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
  # Heartbeat: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for doctor"
  # When completed: WAIT_EXIT = result.exit_code
  [ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "doctor-issue-${ISSUE_NUMBER}"; handle_shutdown; }
  ./.loom/scripts/agent-destroy.sh "doctor-issue-${ISSUE_NUMBER}"

  # Validate doctor phase contract
  ./.loom/scripts/validate-phase.sh doctor "$ISSUE_NUMBER" --pr "$PR_NUMBER" --task-id "$TASK_ID"
  if [ $? -eq 0 ]; then
    echo "Doctor completed, returning to Judge"
    PHASE="judge"
  fi

  DOCTOR_ITERATION=$((DOCTOR_ITERATION + 1))

  # If we've returned to judge phase, run the judge again
  if [ "$PHASE" = "judge" ]; then
    ./.loom/scripts/agent-spawn.sh --role judge --name "judge-issue-${ISSUE_NUMBER}" --args "$PR_NUMBER" --on-demand

    # Non-blocking wait with heartbeat polling
    Bash(command="./.loom/scripts/agent-wait-bg.sh 'judge-issue-${ISSUE_NUMBER}' --timeout 900 --issue '$ISSUE_NUMBER'", run_in_background=true)
    # Poll: TaskOutput(task_id=WAIT_TASK_ID, block=false, timeout=5000)
    # Heartbeat: ./.loom/scripts/report-milestone.sh heartbeat --task-id "$TASK_ID" --action "waiting for judge (doctor loop)"
    # When completed: WAIT_EXIT = result.exit_code
    [ "$WAIT_EXIT" -eq 3 ] && { ./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE_NUMBER}"; handle_shutdown; }
    ./.loom/scripts/agent-destroy.sh "judge-issue-${ISSUE_NUMBER}"

    # Validate judge phase contract
    ./.loom/scripts/validate-phase.sh judge "$ISSUE_NUMBER" --pr "$PR_NUMBER" --task-id "$TASK_ID"
    [ $? -ne 0 ] && { echo "Judge phase contract failed in doctor loop"; exit 1; }

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
  # Check if default mode - stop here, don't merge
  if [ "$DEFAULT_MODE" = "true" ]; then
    echo "Default mode: stopping at loom:pr state"
    gh issue comment $ISSUE_NUMBER --body "**PR approved** - stopping at \`loom:pr\`. Ready for human merge or Champion auto-merge."
    exit 0
  fi

  # Check if --force mode - auto-merge with conflict resolution
  if [ "$FORCE_MODE" = "true" ]; then
    echo "Force mode: auto-merging PR"

    # Use merge-pr.sh for worktree-safe merge via GitHub API
    ./.loom/scripts/merge-pr.sh $PR_NUMBER --cleanup-worktree || {
      echo "Merge failed for PR #$PR_NUMBER"
      exit 1
    }
    echo "PR merged successfully"

    gh issue comment $ISSUE_NUMBER --body "**Auto-merged** PR #$PR_NUMBER via \`/shepherd --force\`"
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

## Prerequisites

The shepherd requires these scripts in `.loom/scripts/`:
- `agent-spawn.sh` — spawn ephemeral tmux worker sessions
- `agent-wait-bg.sh` — wait for worker completion with shutdown signal checking
- `agent-wait.sh` — wait for worker completion (used by `agent-wait-bg.sh` internally)
- `agent-destroy.sh` — clean up worker sessions
- `validate-phase.sh` — validate phase contracts and attempt recovery

No terminal pre-configuration is needed — workers are created on-demand per phase.

## Error Handling Details

### Worker Spawn Failure

If `agent-spawn.sh` fails:

```bash
# Retry once for transient failures
if ! ./.loom/scripts/agent-spawn.sh --role "$ROLE" --name "${ROLE}-issue-${ISSUE}" --args "$ARGS" --on-demand; then
    sleep 5
    if ! ./.loom/scripts/agent-spawn.sh --role "$ROLE" --name "${ROLE}-issue-${ISSUE}" --args "$ARGS" --on-demand; then
        echo "ERROR: Failed to spawn $ROLE worker after retry"
        gh issue edit $ISSUE --add-label "loom:blocked"
        gh issue comment $ISSUE --body "**Orchestration blocked**: Failed to spawn $ROLE worker."
        exit 1
    fi
fi
```

### Worker Timeout

After the polling loop completes, check exit code:

```bash
# WAIT_EXIT is obtained from the TaskOutput polling loop
if [ "$WAIT_EXIT" -eq 3 ]; then
    echo "Shutdown signal detected - cleaning up"
    ./.loom/scripts/agent-destroy.sh "${ROLE}-issue-${ISSUE}"
    handle_shutdown
elif [ "$WAIT_EXIT" -eq 1 ]; then
    echo "Worker timed out - destroying session"
    ./.loom/scripts/agent-destroy.sh "${ROLE}-issue-${ISSUE}" --force
    # Check if the worker made partial progress via labels
fi
```
