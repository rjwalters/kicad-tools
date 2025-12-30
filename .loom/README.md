# Loom Workspace Configuration

> **This project uses [Loom](https://github.com/rjwalters/loom)** - A multi-terminal desktop application that orchestrates AI-powered development agents using git worktrees and GitHub as the coordination layer.

This directory contains workspace-specific Loom configuration that should be committed to version control for team sharing.

## Files in This Directory

### `config.json` - Terminal/Agent Configurations
**Commit this file!** Contains:
- Agent terminal configurations
- Next agent number (monotonic counter)
- Role assignments
- Autonomous mode settings

### `roles/` - Custom Role Definitions
**Commit these files!** Team-specific roles:
- `*.md` - Role definition markdown (required)
- `*.json` - Role metadata (optional)

Custom roles override system defaults when they have the same filename.

## What Gets Committed vs Ignored

### ✅ Commit These (Shared with Team)
```
.loom/
├── config.json          # Agent configurations
├── roles/               # Custom roles
│   ├── my-role.md
│   └── my-role.json
└── README.md            # This file
```

### ❌ Don't Commit These (Runtime State)
These are automatically gitignored:
```
.loom/
├── .daemon.pid          # Dev script PID file
├── .daemon.log          # Dev script logs
├── daemon.sock          # IPC socket
├── state.json           # Runtime terminal state
├── activity.db          # Activity tracking database
└── worktrees/           # Git worktrees (one per issue)
```

Note: Production daemon logs are written to `~/.loom/daemon.log` (home directory).

## Creating Custom Roles

### Step 1: Create Role Definition

Create `.loom/roles/my-role.md`:

```markdown
# My Custom Role

You are a specialist in the {{workspace}} repository.

## Your Role

Describe what this role does...

## Guidelines

- Guideline 1
- Guideline 2
```

Template variables:
- `{{workspace}}` - Replaced with workspace path

### Step 2: Add Metadata (Optional)

Create `.loom/roles/my-role.json`:

```json
{
  "name": "My Custom Role",
  "description": "Brief description",
  "defaultInterval": 0,
  "defaultIntervalPrompt": "Continue working",
  "autonomousRecommended": false,
  "suggestedWorkerType": "claude"
}
```

### Step 3: Use the Role

1. Right-click terminal → Settings
2. Select "my-role.md" from role dropdown
3. Configure autonomous mode if desired
4. Save

See [../defaults/roles/README.md](../defaults/roles/README.md) for detailed role creation guide.

## Customizing Agent Configuration

Edit `config.json` to customize:

```json
{
  "nextAgentNumber": 3,
  "agents": [
    {
      "id": "1",
      "name": "Worker 1",
      "status": "idle",
      "isPrimary": true,
      "role": "claude-code-worker",
      "roleConfig": {
        "workerType": "claude",
        "roleFile": "worker.md",
        "targetInterval": 300000,
        "intervalPrompt": "Continue working on tasks"
      }
    }
  ]
}
```

**Important**: The `nextAgentNumber` is monotonic - it only increases, never decreases. Deleted agents' numbers are never reused.

## Factory Reset

To restore default configuration:

1. **File** → **Factory Reset Workspace...**
2. Confirm the operation
3. All `.loom/` contents will be deleted
4. Default configuration will be restored from `defaults/`

**Warning**: This deletes all custom roles and configurations!

## Sharing Configuration

When working in a team:

1. **Do commit**: Custom roles, agent configurations
2. **Team members get**: Your custom roles and suggested agent setup
3. **Each developer has**: Their own runtime state (daemon, worktrees)

This allows teams to share agent roles and configurations while keeping runtime state local.

## Troubleshooting

### Custom roles not showing up
- Check filename ends with `.md`
- Check file is in `.loom/roles/` directory
- Restart Loom to reload roles

### Configuration not persisting
- Check `.loom/config.json` exists
- Check file permissions (should be writable)
- Check logs: `.loom/.daemon.log`

### Worktrees taking up space
- Worktrees are automatically cleaned up when terminals are destroyed
- Manual cleanup: **File** → **Factory Reset Workspace**
- Or delete `.loom/worktrees/` directory manually

## More Information

- System roles: [../defaults/roles/](../defaults/roles/)
- Role creation guide: [../defaults/roles/README.md](../defaults/roles/README.md)
- Workflow guide: [../WORKFLOWS.md](../WORKFLOWS.md)
- Development guide: [../CLAUDE.md](../CLAUDE.md)
