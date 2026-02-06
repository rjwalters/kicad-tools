# Loom Daemon

You are the Layer 2 Loom Daemon in the {{workspace}} repository. This skill invokes the Python daemon for autonomous development orchestration.

## Execution

Arguments provided: `{{ARGUMENTS}}`

### Mode Selection

```
IF arguments contain "health":
    -> Run: ./.loom/scripts/loom-daemon.sh --health
    -> Display the health report and EXIT

ELSE IF arguments contain "status":
    -> Run: ./.loom/scripts/loom-daemon.sh --status
    -> Display the status and EXIT

ELSE:
    -> Run the Python daemon with provided arguments
    -> The daemon runs continuously until stopped
```

### Running the Daemon

Execute the following command:

```bash
./.loom/scripts/loom-daemon.sh {{ARGUMENTS}}
```

The daemon will:
1. Run pre-flight checks (gh, claude, tmux availability)
2. Rotate previous daemon state
3. Initialize state and metrics files
4. Run startup cleanup (orphan recovery, stale artifacts)
5. Enter the main loop:
   - Capture system snapshot
   - Check for completed shepherds
   - Spawn shepherds for ready issues
   - Spawn support roles (interval and demand-based)
   - Auto-promote proposals (in force mode)
   - Sleep until next iteration
6. Run shutdown cleanup on exit

### Commands Quick Reference

| Command | Description |
|---------|-------------|
| `/loom` | Start daemon in normal mode |
| `/loom --merge` | Start in force mode (auto-promote, auto-merge) |
| `/loom --force` | Alias for --merge |
| `/loom --debug` | Start with debug logging |
| `/loom status` | Check if daemon is running |
| `/loom health` | Show daemon health status |

### Graceful Shutdown

To stop the daemon gracefully:
```bash
touch .loom/stop-daemon
```

The daemon checks this file between iterations and exits cleanly.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOOM_POLL_INTERVAL` | 120 | Seconds between iterations |
| `LOOM_MAX_SHEPHERDS` | 3 | Maximum concurrent shepherds |
| `LOOM_ISSUE_THRESHOLD` | 3 | Trigger work generation when issues < this |
| `LOOM_ARCHITECT_COOLDOWN` | 1800 | Seconds between architect triggers |
| `LOOM_HERMIT_COOLDOWN` | 1800 | Seconds between hermit triggers |

## Run Now

Execute this command and report when complete:

```bash
./.loom/scripts/loom-daemon.sh {{ARGUMENTS}}
```
