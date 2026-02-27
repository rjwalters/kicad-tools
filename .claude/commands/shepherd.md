# Shepherd

Orchestrate a single issue through its full lifecycle as a **signal-writer and observer** — you coordinate the shepherd process through JSON signals and state observation. You NEVER spawn shepherd or worker processes directly via Bash.

## Arguments

**Arguments**: $ARGUMENTS

Parse the issue number and any flags from the arguments.

## Supported Options

| Flag | Description |
|------|-------------|
| `--merge` or `-m` | Auto-approve, resolve conflicts, auto-merge after approval. Also overrides `loom:blocked` status. |
| `--to <phase>` | Stop after specified phase (curated, pr, approved) |
| `--task-id <id>` | Continue from previous checkpoint |

**Deprecated options** (still work with deprecation warnings):
- `--force` or `-f` - Use `--merge` or `-m` instead
- `--force-pr` - Now the default behavior
- `--force-merge` - Use `--merge` or `-m` instead
- `--wait` - No longer blocks; shepherd always exits after PR approval

## Examples

```bash
/shepherd 123                    # Exit after PR approval (default)
/shepherd 123 --merge            # Fully automated, auto-merge after review
/shepherd 123 -m                 # Same as above (short form)
/shepherd 123 --to curated       # Stop after curation phase
```

## Execution

### Step 1: Daemon Detection

Check whether the standalone daemon is running:

```bash
PID=$(cat .loom/daemon-loop.pid 2>/dev/null)
kill -0 "$PID" 2>/dev/null && echo "RUNNING" || echo "NOT RUNNING"
```

**If the daemon is NOT running**, display this message and EXIT:

```
The Loom daemon is not running.

Start it from a terminal OUTSIDE Claude Code:

  ./.loom/scripts/daemon.sh start                      # Normal mode

Then run /shepherd <issue> again.
To use merge mode, pass --merge when running /shepherd (e.g. /shepherd 123 --merge).

Why run outside Claude Code?
  Shepherd and worker sessions start as daemon children (not Claude Code
  descendants), avoiding the nested Claude Code spawning restriction.
```

**If the daemon IS running**, proceed to Step 2.

### Step 2: Check for Existing Shepherd

Before writing a new signal, check whether this issue is already being shepherded.
Read `.loom/daemon-state.json` and look for a shepherd slot with `issue == <N>` and `status == "working"`.

If found, skip to Step 5 (monitor the existing shepherd using its `task_id`).

### Step 3: Verify Issue is Open

Before writing the spawn signal, check that the issue is still open:

```bash
gh issue view <N> --json state --jq '.state'
```

**If the issue is CLOSED**, display this message and EXIT:

```
Issue #<N> is closed.

The shepherd cannot be spawned for a closed issue.
To proceed, reopen the issue first:

  gh issue reopen <N>

Then run /shepherd <N> again.
```

**If the issue is OPEN**, proceed to Step 4.

### Step 4: Write Spawn Signal

Use the **Write tool** (not Bash) to create a signal file at:

```
.loom/signals/cmd-{YYYYMMDD-HHMMSS}-{random4hex}.json
```

Payload format:

```json
{
  "action": "spawn_shepherd",
  "issue": <N>,
  "mode": "<default|force>",
  "flags": []
}
```

**Argument mapping:**

| Argument | Signal field |
|----------|-------------|
| (none) | `"mode": "default"` |
| `--merge` or `-m` | `"mode": "force"` |
| `--to <phase>` | `"flags": ["--to", "<phase>"]` |
| `--task-id <id>` | `"flags": ["--task-id", "<id>"]` |
| `--force` / `-f` | `"mode": "force"` (deprecated alias) |

Example for `/shepherd 123 --merge`:
```json
{"action": "spawn_shepherd", "issue": 123, "mode": "force", "flags": []}
```

Example for `/shepherd 123 --to curated`:
```json
{"action": "spawn_shepherd", "issue": 123, "mode": "default", "flags": ["--to", "curated"]}
```

The daemon polls `.loom/signals/` every 2 seconds and processes commands atomically.

### Step 5: Confirm Daemon Pickup

After writing the signal, read `.loom/daemon-state.json` every ~5 seconds until a shepherd slot shows `issue == <N>` with a `task_id`. This confirms the daemon received and processed the signal.

**If no pickup after 30 seconds**: Check whether the signal file still exists in `.loom/signals/` — if present, the daemon hasn't processed it yet (may be busy or stopped). Report to the user and suggest checking daemon status with `.loom/scripts/daemon.sh status`.

### Step 6: Monitor Progress

Once the `task_id` is known, read `.loom/progress/shepherd-{task_id}.json` every 10–15 seconds. Report milestone events to the user as they arrive:

| Milestone event | Message to show user |
|----------------|---------------------|
| `phase_entered: curator` | `→ Curator phase: enhancing issue...` |
| `phase_entered: builder` | `→ Builder phase: implementing...` |
| `worktree_created` | `  Worktree created` |
| `first_commit` | `  First commit made` |
| `pr_created` | `→ PR #M created` |
| `phase_entered: judge` | `→ Judge phase: reviewing PR...` |
| `phase_entered: doctor` | `→ Doctor phase: addressing feedback...` |
| `completed` | `✅ Shepherd complete` |
| `error` | `❌ Error: <message>` |

You can also use `mcp__loom__get_terminal_output` with the shepherd's terminal ID (from `daemon-state.json`) to show live output on request.

### Step 7: Stuck Detection and Intervention

If `heartbeat_age_seconds > 120` in the progress file (or `heartbeat_stale: true`):

1. Inform the user: "⚠️ Worker appears stuck (no heartbeat for Xs)"
2. Use `mcp__loom__send_terminal_input` to send a gentle nudge to the worker terminal
3. Wait 30s — if still no heartbeat, escalate to the user for manual intervention

### Step 8: Report Outcome

When the progress file shows `status: completed` or `status: error`, summarize the result:

- **Completed**: PR number, merge status, total duration
- **Error**: Phase where it failed, error message, suggested recovery steps
- **Blocked**: Label state, reason, what human action is needed

## Reference Documentation

For detailed orchestration workflow, phase definitions, and troubleshooting:
- **Lifecycle details**: `.claude/commands/shepherd-lifecycle.md`
- **Daemon startup**: `.loom/scripts/daemon.sh --help`
- **Daemon status**: `.loom/scripts/daemon.sh status`
- **Signal protocol**: `loom-tools/src/loom_tools/daemon_v2/command_poller.py`
- **Python shepherd**: `loom-tools/src/loom_tools/shepherd/`
