# Loom Daemon - Reference Documentation

This file contains detailed reference documentation for the Loom daemon. It is NOT loaded by agents during normal operation - it is for human reference only.

For daemon execution instructions, see:
- `loom.md` - Mode detection and routing
- `loom-parent.md` - Parent loop mode instructions
- `loom-iteration.md` - Iteration mode instructions

## State File Format

The daemon maintains state in `.loom/daemon-state.json`. This file provides comprehensive information for debugging, crash recovery, and system observability.

### Complete State Structure

```json
{
  "started_at": "2026-01-23T10:00:00Z",
  "last_poll": "2026-01-23T11:30:00Z",
  "running": true,
  "iteration": 42,
  "force_mode": false,
  "debug_mode": false,
  "daemon_session_id": "1706400000-12345",

  "shepherds": {
    "shepherd-1": {
      "status": "working",
      "issue": 123,
      "task_id": "abc123",
      "output_file": "/tmp/claude/.../abc123.output",
      "started": "2026-01-23T10:15:00Z",
      "last_phase": "builder",
      "pr_number": null
    },
    "shepherd-2": {
      "status": "idle",
      "issue": null,
      "task_id": null,
      "output_file": null,
      "idle_since": "2026-01-23T11:00:00Z",
      "idle_reason": "no_ready_issues",
      "last_issue": 100,
      "last_completed": "2026-01-23T10:58:00Z"
    },
    "shepherd-3": {
      "status": "working",
      "issue": 456,
      "task_id": "def456",
      "output_file": "/tmp/claude/.../def456.output",
      "started": "2026-01-23T10:45:00Z",
      "last_phase": "judge",
      "pr_number": 789
    }
  },

  "support_roles": {
    "architect": {
      "status": "idle",
      "task_id": null,
      "output_file": null,
      "last_completed": "2026-01-23T09:30:00Z",
      "last_result": "created_proposal",
      "proposals_created": 2
    },
    "hermit": {
      "status": "running",
      "task_id": "ghi789",
      "output_file": "/tmp/claude/.../ghi789.output",
      "started": "2026-01-23T11:00:00Z"
    },
    "guide": {
      "status": "running",
      "task_id": "jkl012",
      "output_file": "/tmp/claude/.../jkl012.output",
      "started": "2026-01-23T10:05:00Z"
    },
    "champion": {
      "status": "running",
      "task_id": "mno345",
      "output_file": "/tmp/claude/.../mno345.output",
      "started": "2026-01-23T10:10:00Z",
      "prs_merged_this_session": 2
    },
    "doctor": {
      "status": "idle",
      "task_id": null,
      "output_file": null,
      "last_completed": "2026-01-23T10:30:00Z"
    },
    "auditor": {
      "status": "idle",
      "task_id": null,
      "output_file": null,
      "last_completed": "2026-01-23T09:00:00Z"
    }
  },

  "pipeline_state": {
    "ready": ["#1083", "#1080"],
    "building": ["#1044"],
    "review_requested": ["PR #1056"],
    "changes_requested": ["PR #1059"],
    "ready_to_merge": ["PR #1058"],
    "blocked": [
      {
        "type": "pr",
        "number": 1059,
        "reason": "merge_conflicts",
        "detected_at": "2026-01-23T11:20:00Z"
      }
    ],
    "last_updated": "2026-01-23T11:30:00Z"
  },

  "warnings": [
    {
      "time": "2026-01-23T11:10:00Z",
      "type": "blocked_pr",
      "severity": "warning",
      "message": "PR #1059 has merge conflicts",
      "context": {
        "pr_number": 1059,
        "issue_number": 1044,
        "requires_role": "doctor"
      },
      "acknowledged": false
    },
    {
      "time": "2026-01-23T10:30:00Z",
      "type": "shepherd_error",
      "severity": "info",
      "message": "shepherd-1 encountered rate limit, retrying",
      "context": {
        "shepherd_id": "shepherd-1",
        "issue": 123
      },
      "acknowledged": true
    }
  ],

  "completed_issues": [100, 101, 102],
  "total_prs_merged": 3,
  "last_architect_trigger": "2026-01-23T10:00:00Z",
  "last_hermit_trigger": "2026-01-23T10:30:00Z",

  "force_mode_auto_promotions": [
    {"issue": 123, "type": "architect", "time": "2026-01-24T10:05:00Z"},
    {"issue": 456, "type": "curated", "time": "2026-01-24T10:10:00Z"}
  ],

  "session_limit_awareness": {
    "enabled": true,
    "last_check": "2026-01-23T11:30:00Z",
    "session_percent": 45,
    "paused_for_rate_limit": false,
    "pause_started_at": null,
    "expected_resume_at": null,
    "session_percent_at_pause": null,
    "total_pauses": 0,
    "total_pause_duration_minutes": 0
  },

  "stuck_detection": {
    "enabled": true,
    "last_check": "2026-01-23T11:30:00Z",
    "config": {
      "idle_threshold": 600,
      "working_threshold": 1800,
      "loop_threshold": 3,
      "error_spike_threshold": 5,
      "intervention_mode": "escalate"
    },
    "active_interventions": [],
    "recent_detections": [
      {
        "agent_id": "shepherd-1",
        "issue": 123,
        "detected_at": "2026-01-23T11:25:00Z",
        "severity": "warning",
        "indicators": ["no_progress:720s"],
        "intervention": "alert",
        "resolved_at": null
      }
    ],
    "total_detections": 1,
    "total_interventions": 1,
    "false_positive_rate": 0.1
  },

  "stale_detection": {
    "last_check": "2026-01-25T10:00:00Z",
    "last_recovered": [123, 456],
    "total_recovered": 5,
    "check_interval": 10
  },

  "cleanup": {
    "lastRun": "2026-01-23T11:00:00Z",
    "lastEvent": "periodic",
    "lastCleaned": ["issue-98", "issue-99"],
    "pendingCleanup": [],
    "errors": []
  }
}
```

### State Field Reference

#### Shepherd Status Values

| Status | Description |
|--------|-------------|
| `working` | Actively processing an issue |
| `idle` | No issue assigned, waiting for work |
| `errored` | Encountered an error, may need intervention |
| `paused` | Manually paused via signal or stuck detection |

#### Shepherd Idle Reasons

| Reason | Description |
|--------|-------------|
| `no_ready_issues` | No issues with `loom:issue` label available |
| `at_capacity` | All shepherd slots filled |
| `completed_issue` | Just finished an issue, waiting for next |
| `rate_limited` | Paused due to API rate limits |
| `shutdown_signal` | Paused due to graceful shutdown |

#### Warning Types

| Type | Severity | Description |
|------|----------|-------------|
| `blocked_pr` | warning | PR has merge conflicts or failed checks |
| `shepherd_error` | info/warning | Shepherd encountered recoverable error |
| `role_failure` | error | Support role failed to complete |
| `rate_limit` | info | Rate limit encountered, will retry |
| `stuck_agent` | warning | Agent detected as stuck |
| `dependency_blocked` | warning | Issue blocked on unresolved dependency |
| `spawn_failed` | warning | Task spawn failed verification |
| `stale_building_recovered` | info | Orphaned building issue recovered |

#### Pipeline State Fields

| Field | Content |
|-------|---------|
| `ready` | Issues with `loom:issue` label, ready for shepherds |
| `building` | Issues with `loom:building` label, actively being worked |
| `review_requested` | PRs with `loom:review-requested` label |
| `changes_requested` | PRs with `loom:changes-requested` label |
| `ready_to_merge` | PRs with `loom:pr` label, approved by Judge |
| `blocked` | Items that need attention (conflicts, failures, etc.) |

## Stuck Detection Configuration

### Stuck Indicators

| Indicator | Default Threshold | Description |
|-----------|-------------------|-------------|
| `no_progress` | 10 minutes | No output written to task output file |
| `extended_work` | 30 minutes | Working on same issue without creating PR |
| `looping` | 3 occurrences | Repeated similar error patterns |
| `error_spike` | 5 errors | Multiple errors in short period |

### Intervention Types

| Type | Trigger | Action |
|------|---------|--------|
| `alert` | Low severity (warning) | Write to `.loom/interventions/`, human reviews |
| `suggest` | Medium severity (elevated) | Suggest role switch (e.g., Builder -> Doctor) |
| `pause` | High severity (critical) | Auto-pause via signal.sh, requires manual restart |
| `clarify` | Error spike | Suggest requesting clarification from issue author |
| `escalate` | Critical + multiple indicators | Full escalation: pause + alert + loom:blocked label |

### Configuring Stuck Detection

```bash
# Configure thresholds
./.loom/scripts/stuck-detection.sh configure \
  --idle-threshold 900 \
  --working-threshold 2400 \
  --intervention-mode escalate

# View current configuration
./.loom/scripts/stuck-detection.sh status

# Check specific agent
./.loom/scripts/stuck-detection.sh check-agent shepherd-1 --verbose
```

### Intervention Files

When interventions are triggered, files are created in `.loom/interventions/`:

```
.loom/interventions/
+-- shepherd-1-20260124120000.json  # Full detection data
+-- shepherd-1-latest.txt           # Human-readable summary
+-- shepherd-2-20260124121500.json
+-- shepherd-2-latest.txt
```

## Stale Building Detection

### Detection Sources

The script cross-references three sources to detect orphaned work:

| Source | What It Checks | Orphan Signal |
|--------|---------------|---------------|
| GitHub Labels | `loom:building` issues | Issue has building label |
| Worktrees | `.loom/worktrees/issue-N` | No worktree for issue |
| Open PRs | `feature/issue-N` branch | No PR referencing issue |

If **all three** indicate no active work and issue is >2 hours old -> **orphaned**.

### Recovery Actions

| Condition | Recovery Action |
|-----------|-----------------|
| No worktree, no PR (>2h) | Reset to `loom:issue`, add recovery comment |
| Has PR with `loom:changes-requested` | Transition to `loom:blocked` |
| Has PR but stale (>24h) | Flag only (needs manual review) |

### Configuration

```bash
# Environment variables for thresholds
STALE_THRESHOLD_HOURS=2       # Hours before no-PR issue is stale
STALE_WITH_PR_HOURS=24        # Hours before stale-PR issue is flagged

# Run manually to check status
./.loom/scripts/stale-building-check.sh --verbose

# Auto-recover (run by daemon)
./.loom/scripts/stale-building-check.sh --recover

# JSON output for integration
./.loom/scripts/stale-building-check.sh --json
```

## Session Rotation

When a new daemon session starts, the existing `daemon-state.json` is automatically rotated to preserve session history:

```
.loom/
+-- daemon-state.json          # Current session (always this name)
+-- 00-daemon-state.json       # First archived session
+-- 01-daemon-state.json       # Second archived session
+-- 02-daemon-state.json       # Third archived session
```

**Why session rotation?**
- Debugging patterns across multiple sessions
- Analyzing daemon behavior over time
- Post-mortem analysis when issues occur
- Understanding long-term trends in the development pipeline

**Configuration:**
- `LOOM_MAX_ARCHIVED_SESSIONS` - Maximum sessions to keep (default: 10)

**Commands:**
```bash
# Preview session rotation
./.loom/scripts/rotate-daemon-state.sh --dry-run

# Manually prune old sessions
./.loom/scripts/daemon-cleanup.sh prune-sessions

# Keep more archived sessions
./.loom/scripts/rotate-daemon-state.sh --max-sessions 20
```

Archived sessions include a `session_summary` field with final statistics:
```json
{
  "session_summary": {
    "session_id": 5,
    "archived_at": "2026-01-24T15:30:00Z",
    "issues_completed": 12,
    "prs_merged": 10,
    "total_iterations": 156
  }
}
```

## Crash Recovery

On daemon restart, use the enhanced state for recovery:

```python
def recover_from_crash():
    """Recover daemon state after unexpected shutdown."""

    state = load_daemon_state()

    if not state.get("running"):
        print("State shows clean shutdown, starting fresh")
        return

    print("Recovering from crash...")

    # Check each shepherd's last known state
    for shepherd_id, shepherd_state in state["shepherds"].items():
        if shepherd_state.get("status") == "working":
            issue = shepherd_state.get("issue")
            last_phase = shepherd_state.get("last_phase", "unknown")

            print(f"  {shepherd_id} was working on #{issue} (phase: {last_phase})")

            # Check if PR was created
            if shepherd_state.get("pr_number"):
                pr = shepherd_state["pr_number"]
                if pr_is_merged(pr):
                    print(f"    PR #{pr} is merged - marking complete")
                    mark_complete(shepherd_id, issue)
                else:
                    print(f"    PR #{pr} exists - resuming from judge phase")
                    resume_shepherd(shepherd_id, issue, from_phase="judge")
            else:
                # No PR, check issue state
                labels = get_issue_labels(issue)
                if "loom:building" in labels:
                    print(f"    Issue still building - resuming shepherd")
                    resume_shepherd(shepherd_id, issue, from_phase=last_phase)
                else:
                    print(f"    Issue state changed externally - releasing shepherd")
                    release_shepherd(shepherd_id)

    # Review warnings for actionable items
    for warning in state.get("warnings", []):
        if not warning.get("acknowledged") and warning["severity"] == "error":
            print(f"  Unacknowledged error: {warning['message']}")
```

## Cleanup Integration

The daemon integrates with cleanup scripts to manage task artifacts and worktrees safely.

### Cleanup Events

| Event | When | What Gets Cleaned |
|-------|------|-------------------|
| `shepherd-complete` | After shepherd finishes issue | Task outputs archived, worktree (if PR merged) |
| `daemon-startup` | When daemon starts | Stale artifacts from previous session |
| `daemon-shutdown` | Before daemon exits | Archive task outputs |
| `periodic` | Configurable interval | Conservative cleanup respecting active shepherds |

### Cleanup Scripts

```bash
# Archive task outputs to .loom/logs/{date}/
./.loom/scripts/archive-logs.sh [--dry-run] [--retention-days N]

# Safe worktree cleanup (only MERGED PRs)
./.loom/scripts/safe-worktree-cleanup.sh [--dry-run] [--grace-period N]

# Event-driven daemon cleanup
./.loom/scripts/daemon-cleanup.sh <event> [options]
```

### Cleanup Configuration

Configure via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LOOM_CLEANUP_ENABLED` | true | Enable/disable cleanup |
| `LOOM_ARCHIVE_LOGS` | true | Archive logs before deletion |
| `LOOM_RETENTION_DAYS` | 7 | Days to retain archives |
| `LOOM_CLEANUP_INTERVAL` | 360 | Minutes between periodic cleanups |
| `LOOM_GRACE_PERIOD` | 600 | Seconds after PR merge before cleanup |

## Status Report Format

When queried for status via `/loom status`:

```
====================================================================
  LOOM DAEMON STATUS - FULLY AUTONOMOUS
====================================================================

Role: Loom Daemon (Layer 2)
Status: Running (iteration 156)
Uptime: 2h 15m
Mode: FULLY AUTONOMOUS

SYSTEM STATE (auto-managed):
  Ready issues (loom:issue):     5  [threshold: 3]
  Building (loom:building):      2
  PRs pending review:            2
  PRs ready to merge (loom:pr):  1

HUMAN APPROVAL QUEUE:
  Curated issues:                3  <- Human approves -> loom:issue
  Architect proposals:           2  <- Human approves -> loom:issue
  Hermit proposals:              1  <- Human approves -> loom:issue
  Blocked issues:                0  <- Human intervenes

SHEPHERDS (auto-spawned): 2/3 active
  shepherd-1: Issue #123 (45m) [task:abc123]
  shepherd-2: Issue #456 (12m) [task:def456]
  shepherd-3: idle -> will auto-spawn when ready issues available

SUPPORT ROLES (auto-managed):
  Architect: idle (last: 28m ago, cooldown: 30m, proposals: 2/2 max)
  Hermit:    running [task:ghi789] (started: 5m ago)
  Guide:     running [task:jkl012] (idle 8m, interval: 15m)
  Champion:  running [task:mno345] (idle 3m, interval: 10m)
  Doctor:    idle (last: 30m ago)
  Auditor:   idle (last: 2h ago)

SESSION STATS:
  Issues completed: 3
  PRs merged: 3
  Architect triggers: 4
  Hermit triggers: 2

WORK GENERATION (auto-triggered):
  Last Architect: 28m ago (cooldown: 30m) -> ready to trigger if backlog low
  Last Hermit:    45m ago (cooldown: 30m) -> ready to trigger if backlog low

AUTONOMOUS DECISIONS (no human required):
  Shepherd spawning (when ready issues > 0)
  Architect triggering (when backlog < 3)
  Hermit triggering (when backlog < 3)
  Guide respawning (every 15m)
  Champion respawning (every 10m)
  Doctor respawning (every 5m)
  Auditor respawning (every 10m)

HUMAN ACTIONS (when you want to):
  - Approve proposals: gh issue edit N --add-label loom:issue
  - Unblock issues: gh issue edit N --remove-label loom:blocked
  - Stop daemon: touch .loom/stop-daemon
====================================================================
```

## Role Validation

### Role Dependencies

Roles have dependencies on other roles to handle specific label transitions:

| Role | Creates Label | Requires Role | To Handle |
|------|---------------|---------------|-----------|
| Champion | `loom:changes-requested` | Doctor | Address PR feedback |
| Builder | `loom:review-requested` | Judge | Review PRs |
| Curator | `loom:curated` | Champion (or human) | Promote to `loom:issue` |
| Judge | `loom:pr` | Champion | Auto-merge approved PRs |
| Judge | `loom:changes-requested` | Doctor | Address feedback |

### Validation Script

```bash
# Validate role configuration
./.loom/scripts/validate-roles.sh

# Output:
# Configured roles: builder, champion, curator, hermit, judge
# WARNINGS:
#   - champion -> doctor: PRs with loom:changes-requested will get stuck
#   - judge -> doctor: PRs with loom:changes-requested will get stuck

# JSON output for automation
./.loom/scripts/validate-roles.sh --json
```

### Validation Modes

| Mode | Behavior |
|------|----------|
| `--warn` (default) | Log warnings, continue startup |
| `--strict` | Fail startup if any warnings |
| `--ignore` | Skip validation entirely |

Configure via environment variable:

```bash
export LOOM_VALIDATION_MODE=strict
/loom
```

## Terminal/Subagent Configuration

### Manual Orchestration Mode (Claude Code CLI)

In MOM, the daemon spawns subagents using the Task tool. No pre-configured terminals needed.

| Subagent Pool | Max | Purpose |
|---------------|-----|---------|
| Shepherds | 3 | Issue lifecycle orchestration |
| Architect | 1 | Work generation (feature proposals) |
| Hermit | 1 | Work generation (simplification proposals) |
| Guide | 1 | Backlog triage and prioritization |
| Champion | 1 | Auto-merge approved PRs |
| Doctor | 1 | PR conflict resolution |
| Auditor | 1 | Main branch validation |

### Required Terminal Configuration for Tauri App Mode

| Terminal ID | Role | Purpose |
|-------------|------|---------|
| shepherd-1, shepherd-2, shepherd-3 | shepherd.md | Issue orchestration pool |
| terminal-architect | architect.md | Work generation (proposals) |
| terminal-hermit | hermit.md | Simplification proposals |
| terminal-guide | guide.md | Backlog triage (always running) |
| terminal-champion | champion.md | Auto-merge (always running) |
| terminal-doctor | doctor.md | PR conflict resolution (always running) |
| terminal-auditor | auditor.md | Main branch validation (always running) |
