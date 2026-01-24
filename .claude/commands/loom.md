# Loom

Assume the Loom Daemon role and run the **continuous** Layer 2 system orchestrator.

## Process

1. **Read the role definition**: Load `.loom/roles/loom.md` or `defaults/roles/loom.md`
2. **Initialize state**: Load or create `.loom/daemon-state.json`
3. **Run continuous loop**: Spawn shepherd subagents, monitor progress, scale pool
4. **Run until cancelled**: Continue until Ctrl+C or stop signal

## Work Scope

As the **Loom Daemon** (Layer 2), you **continuously** orchestrate the system:

- **Spawn shepherds** as background subagents via Task tool
- **Monitor progress** by checking issue states and task outputs
- **Scale the pool** based on ready issue count (up to MAX_SHEPHERDS)
- **Track state** in `.loom/daemon-state.json` for crash recovery

You don't shepherd issues yourself - you spawn subagent shepherds to do the work in parallel.

## Usage

```
/loom                     # Start continuous daemon (runs until cancelled)
/loom status              # Report current system state only
/loom spawn 123           # Manually spawn shepherd for issue #123
/loom stop                # Create stop signal for graceful shutdown
```

## Commands

| Command | Description |
|---------|-------------|
| (none) | Start continuous daemon loop |
| `status` | Report system state without starting loop (see Status Command below) |
| `spawn <issue>` | Manually spawn a shepherd subagent for specific issue |
| `stop` | Create `.loom/stop-daemon` for graceful shutdown |

## Status Command

The `status` command is a **read-only observation interface** for Layer 3 (human observer). It displays the current system state without taking any action.

**To run status**, execute the helper script:

```bash
# Display formatted status
./.loom/scripts/loom-status.sh

# Get status as JSON for scripting
./.loom/scripts/loom-status.sh --json
```

**Status shows**:
- Daemon status (running/stopped, uptime)
- System state (issue counts by label)
- Shepherd pool status (active/idle, assigned issues)
- Support role status (Architect, Hermit, Guide, Champion)
- Session statistics (completed issues, PRs merged)
- Available Layer 3 interventions

**Important**: `/loom status` is for Layer 3 observation - it never modifies state.
- `/loom` = Run the daemon (Layer 2 executor role)
- `/loom status` = Observe the system (Layer 3 observer role)

## Continuous Loop

The daemon runs **continuously** until cancelled:

```
while not cancelled:
    1. Check for shutdown signal (.loom/stop-daemon)
    2. Assess system state (ready issues, building, PRs)
    3. Check shepherd completions (closed issues = done)
    4. Spawn new shepherds up to MAX_SHEPHERDS
    5. Print status report
    6. Sleep 30 seconds, repeat
```

## Spawning Shepherds

Use the Task tool with `run_in_background: true` to spawn parallel shepherds:

```python
Task(
    description="Shepherd issue #123",
    prompt="/shepherd 123 --force-merge",
    subagent_type="general-purpose",
    run_in_background=True
)
```

This enables up to 3 shepherds running concurrently.

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_SHEPHERDS` | 3 | Maximum concurrent shepherd subagents |
| `POLL_INTERVAL` | 30s | Time between status checks |

## Status Report

```
═══════════════════════════════════════════════════
  LOOM DAEMON STATUS
═══════════════════════════════════════════════════
  Status: Running
  Uptime: 2h 15m

  System State:
    Ready issues (loom:issue): 5
    Building (loom:building): 2
    PRs pending review: 1

  Shepherds: 2/3 active
    shepherd-1: Issue #123 (running 45m)
    shepherd-2: Issue #456 (running 12m)
    shepherd-3: idle

  Session Stats:
    Issues completed: 3
    PRs merged: 3
═══════════════════════════════════════════════════
```

## State Persistence

State tracked in `.loom/daemon-state.json`:

```json
{
  "started_at": "2026-01-23T10:00:00Z",
  "running": true,
  "shepherds": {
    "shepherd-1": {
      "issue": 123,
      "task_id": "abc123",
      "output_file": "/path/to/output"
    }
  },
  "completed_issues": [100, 101, 102]
}
```

## Graceful Shutdown

```bash
# Option 1: Create stop signal
touch .loom/stop-daemon

# Option 2: Use command
/loom stop

# Daemon will:
# 1. Stop spawning new shepherds
# 2. Wait for active shepherds to complete (max 5 min)
# 3. Clean up state
# 4. Exit
```

## Layer 1 vs Layer 2

| Layer | Role | Purpose |
|-------|------|---------|
| Layer 1 | **Shepherd** (`/shepherd 123`) | Orchestrates single issue lifecycle |
| Layer 2 | **Loom Daemon** (`/loom`) | Spawns shepherds, manages pool continuously |

Use `/shepherd` to shepherd a specific issue. Use `/loom` to run the continuous orchestrator that manages multiple shepherds.

ARGUMENTS: $ARGUMENTS
