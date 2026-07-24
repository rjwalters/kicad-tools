# Loom Role Definitions

This directory contains role definitions for Loom terminal configurations.

## Source of Truth

**The single source of truth for all Loom role definitions is `.claude/commands/loom/*.md`.**

This directory contains:
- **Symlinks** (`*.md`) pointing to `../.claude/commands/loom/*.md` for backward compatibility
- **Metadata files** (`*.json`) with default settings for each role

### Why Symlinks?

- **Claude Code CLI** uses `.claude/commands/loom/` for slash commands. Subdirectory commands are invoked in the namespaced `/loom:<role>` form (e.g., `/loom:builder`, `/loom:loom`) as of Claude Code 2.1+ (see #3345)
- **Daemon and tooling** that historically read role files from `.loom/roles/` continue to work without code changes
- Symlinks ensure both access the same content - single source of truth

### Editing Roles

To edit a role definition:
1. Edit the file in `.claude/commands/loom/<role>.md`
2. The symlink in `roles/<role>.md` automatically reflects changes

## Available Roles

| Role | Purpose | Autonomous |
|------|---------|------------|
| `architect` | System architecture proposals | 15min |
| `auditor` | Main branch build/runtime validation | 10min |
| `builder` | Feature implementation | Manual |
| `champion` | Proposal evaluation and PR auto-merge | 10min |
| `curator` | Issue enhancement | 5min |
| `doctor` | Bug fixes and PR feedback | Manual |
| `driver` | Plain shell environment | Manual |
| `guide` | Issue triage and prioritization | 15min |
| `hermit` | Code simplification proposals | 15min |
| `judge` | Code review | 5min |
| `loom` | Tier 2 daemon-mode operator surface | 1min |

> **Note**: the `shepherd` role (Layer 1 issue-lifecycle orchestrator) was
> removed in v0.10.0. Use `/loom:sweep <issue>` for the same single-issue
> lifecycle, or `mcp__loom__dispatch_sweep` against the Rust `loom-daemon` for
> multi-account dispatch — see
> [the migration guide](../../docs/migration/v0.10.0-shepherd-deprecation.md).

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
- **`suggestedModel`** (string): Default model alias for this role (`haiku`, `sonnet`, `opus`) or a pinned model ID; the role-default tier of the model-selection precedence chain
- **`defaultInterval`** (number): Default interval in milliseconds (0 = disabled)
- **`defaultIntervalPrompt`** (string): Default prompt sent at each interval
- **`autonomousRecommended`** (boolean): Whether autonomous mode is recommended
- **`suggestedWorkerType`** (string): "claude" or "codex"
- **`gitIdentity`** (object): `name` / `email` used for commits made by this role
- **`stuckThresholds`** (object): Per-role stuck-detection limits (e.g. `maxNoOutput`, `maxNeedsInput`, in milliseconds)

## Creating Custom Roles

To create a custom role:

1. Create `.claude/commands/loom/my-role.md` with the full role definition
2. Optionally create `roles/my-role.json` with metadata
3. Use it via `/loom:my-role` in Claude Code or reference it from daemon terminal configuration

### Role File Structure

```markdown
# My Custom Role

You are a specialist in this repository...

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
1. `/loom:sweep` (or `mcp__loom__dispatch_sweep` against `loom-daemon`) dispatches worker agents (builder, judge, doctor, curator) for each phase
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

Role prompts are written as plain language ("this repository") rather than
templated paths. Install-time substitution is limited to the `CLAUDE.md`
placeholders handled by the installer (`{{REPO_OWNER}}`, `{{REPO_NAME}}`,
`{{LOOM_VERSION}}`, `{{LOOM_COMMIT}}`, `{{INSTALL_DATE}}`); the `.claude/`
role and agent files are copied verbatim, with no substitution pass. Do not
add unimplemented placeholders such as `{{workspace}}` to role files.

## Default vs Workspace Roles

When installed to a target repository:
- `defaults/.claude/commands/loom/*.md` → copied to `.claude/commands/loom/`
- `defaults/roles/*.md` (symlinks) → copied as files to `.loom/roles/`
- `defaults/roles/*.json` → copied to `.loom/roles/`

The installation process dereferences symlinks, so target repos get regular files (not symlinks).
