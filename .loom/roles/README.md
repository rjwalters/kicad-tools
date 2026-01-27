# Loom Role Definitions

This directory contains role definitions for Loom terminal configurations.

## Source of Truth

**The single source of truth for all Loom role definitions is `.claude/commands/*.md`.**

This directory contains:
- **Symlinks** (`*.md`) pointing to `../.claude/commands/*.md` for Tauri App compatibility
- **Metadata files** (`*.json`) with default settings for each role

### Why Symlinks?

- **Claude Code CLI** uses `.claude/commands/` for slash commands (e.g., `/builder`, `/loom`)
- **Tauri App** reads role files from `.loom/roles/` for terminal configuration
- Symlinks ensure both access the same content - single source of truth

### Editing Roles

To edit a role definition:
1. Edit the file in `.claude/commands/<role>.md`
2. The symlink in `roles/<role>.md` automatically reflects changes
3. Both CLI and Tauri App get the updated content

## Available Roles

| Role | Purpose | Autonomous |
|------|---------|------------|
| `architect` | System architecture proposals | 15min |
| `builder` | Feature implementation | Manual |
| `champion` | Proposal evaluation and PR auto-merge | 10min |
| `curator` | Issue enhancement | 5min |
| `doctor` | Bug fixes and PR feedback | Manual |
| `driver` | Plain shell environment | Manual |
| `guide` | Issue triage and prioritization | 15min |
| `hermit` | Code simplification proposals | 15min |
| `judge` | Code review | 5min |
| `loom` | Layer 2 daemon orchestration | 1min |
| `shepherd` | Layer 1 issue lifecycle orchestration | Manual |

## Metadata Files (*.json)

Each role can have an optional JSON metadata file with default settings:

```json
{
  "name": "Builder",
  "description": "Implements features and fixes",
  "defaultInterval": 0,
  "defaultIntervalPrompt": "",
  "autonomousRecommended": false,
  "suggestedWorkerType": "claude"
}
```

### Metadata Fields

- **`name`** (string): Display name for this role
- **`description`** (string): Brief description
- **`defaultInterval`** (number): Default interval in milliseconds (0 = disabled)
- **`defaultIntervalPrompt`** (string): Default prompt sent at each interval
- **`autonomousRecommended`** (boolean): Whether autonomous mode is recommended
- **`suggestedWorkerType`** (string): "claude" or "codex"

## Creating Custom Roles

To create a custom role:

1. Create `.claude/commands/my-role.md` with the full role definition
2. Optionally create `roles/my-role.json` with metadata
3. Use it via `/my-role` in CLI or select in Tauri App terminal settings

### Role File Structure

```markdown
# My Custom Role

You are a specialist in {{workspace}} repository...

## Your Role
- Primary responsibility
- Secondary responsibility

## Workflow
1. First step
2. Second step

## Guidelines
- Best practices
- Working style
```

### Template Variables

- `{{workspace}}` - Replaced with the absolute path to the workspace directory

## Default vs Workspace Roles

When installed to a target repository:
- `defaults/.claude/commands/*.md` → copied to `.claude/commands/`
- `defaults/roles/*.md` (symlinks) → copied as files to `.loom/roles/`
- `defaults/roles/*.json` → copied to `.loom/roles/`

The installation process dereferences symlinks, so target repos get regular files (not symlinks).
