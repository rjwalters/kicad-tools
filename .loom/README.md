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

### `biome.jsonc` - Linter carve-out for Loom-managed files
**Commit this file!** A nested [Biome](https://biomejs.dev) configuration
(`"root": false`) that takes everything under `.loom/` out of your repo-wide
`biome check .`.

Loom installs machine-managed payload here that your repo neither authors nor
formats: installer-emitted JSON stamps (`config.json`, `install-metadata.json`)
whose indentation will not match your Biome config, and
`scripts/experiments/judge-fanout-workflow.js`, a Claude Code Workflow script
whose legal top-level `return` is a hard **parse error** for any standard-JS
parser. Without this carve-out those files make `biome check .` permanently red
on code you did not write.

A sibling `.claude/biome.jsonc` does the same for the handful of Loom-owned
paths under `.claude/` — but narrowly: it excludes only `settings.json`,
`agents/`, and `commands/loom/`, so **your** files under `.claude/` are still
linted normally.

Delete either file if you would rather lint Loom's payload yourself; the next
install or `resync-installed.sh` will restore it. To keep it deleted, pin the
path in `.loom/resync-ignore`. Note that Biome 1.x has no nested-configuration
support and ignores both files — on 1.x you need an explicit `files.ignore`
entry for `.loom/` in your own config.

## What Gets Committed vs Ignored

### ✅ Commit These (Shared with Team)
```
.loom/
├── config.json          # Agent configurations
├── biome.jsonc          # Biome carve-out for Loom-managed files
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

See the [Loom roles documentation](https://github.com/rjwalters/loom/blob/main/defaults/roles/README.md) for detailed role creation guide.

## Customizing Agent Configuration

Edit `config.json` to customize:

```json
{
  "nextAgentNumber": 3,
  "agents": [
    {
      "id": "1",
      "name": "Builder 1",
      "status": "idle",
      "isPrimary": true,
      "role": "claude-code-worker",
      "roleConfig": {
        "workerType": "claude",
        "roleFile": "builder.md",
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
4. Default configuration will be restored from Loom's bundled defaults

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

- System roles: `.loom/roles/` (local copies) or [Loom defaults](https://github.com/rjwalters/loom/tree/main/defaults/roles)
- Role creation guide: [Loom roles documentation](https://github.com/rjwalters/loom/blob/main/defaults/roles/README.md)
- Workflow guide: [CLAUDE.md](../CLAUDE.md) (in your repository root)
- Loom documentation: [https://github.com/rjwalters/loom](https://github.com/rjwalters/loom)
