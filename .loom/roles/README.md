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

## Completion

**Work completion is detected automatically.**

When you complete your task (apply appropriate end-state labels), the orchestration
layer detects this and terminates the session automatically. No explicit exit command is needed.
```

### Completion Detection

Worker completion is detected automatically through **phase contracts** - the orchestration layer validates that the expected end-state has been achieved (e.g., correct labels applied) and terminates the session.

**How it works:**
1. Shepherds spawn worker agents (builder, judge, doctor, curator) for each phase
2. `validate-phase.sh` checks for phase-specific completion criteria:
   - **Curator**: `loom:curated` label on issue
   - **Builder**: PR with `loom:review-requested` label linked to issue
   - **Judge**: `loom:pr` or `loom:changes-requested` label on PR
   - **Doctor**: `loom:review-requested` label after fixes
3. When the phase contract is satisfied, the session terminates automatically
4. Idle detection provides a fallback if the agent becomes unresponsive

**Benefits of automatic detection:**
- No ambiguity about what "completion" means (it's defined by labels)
- Agents don't need to execute shell commands to signal completion
- Consistent behavior across all worker roles

### Template Variables

- `{{workspace}}` - Replaced with the absolute path to the workspace directory

## Default vs Workspace Roles

When installed to a target repository:
- `defaults/.claude/commands/*.md` → copied to `.claude/commands/`
- `defaults/roles/*.md` (symlinks) → copied as files to `.loom/roles/`
- `defaults/roles/*.json` → copied to `.loom/roles/`

The installation process dereferences symlinks, so target repos get regular files (not symlinks).
