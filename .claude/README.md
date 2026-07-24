# Claude Code Settings

This directory contains Claude Code configuration for the Loom project.

## Files

- **`settings.json`**: Team-wide permissions and settings (committed to git)
- **`settings.local.json`**: Personal preferences (gitignored, create if needed)
- **`../.mcp.json`**: MCP server configuration (at project root, committed to git)
- **`agents/`**: Custom subagent definitions for Loom roles (see below)

## Pre-approved Commands

The `settings.json` file pre-approves common development commands to streamline the AI workflow:

### GitHub CLI (`gh`)
- PR operations: create, edit, view, list, checkout, diff, review, checks
- Issue operations: create, edit, view, list, close
- Workflow runs: view

### Git Operations
- Status, add, commit, push, pull, fetch, merge
- Branch management: checkout, branch, log, diff
- Working tree operations: restore, stash, reset, clean
- Worktree operations: add
- Configuration: config, check-ignore

### Package Management
- `pnpm daemon:dev` - Run daemon in dev mode
- `pnpm daemon:build` - Build daemon (release)
- `pnpm check:all` - Run all checks
- `pnpm check:ci` - Run CI checks locally

### Code Quality
- `pnpm clippy` - Rust linting
- `pnpm test` - Run tests

### Rust/Cargo
- `cargo check` - Check compilation
- `cargo build` - Build project
- `cargo test` - Run tests

### Utilities
- File operations: cat, ls, pwd, cd, mkdir
- Image conversion: convert, magick, iconutil
- Terminal management: tmux list-sessions
- Web search: Enabled

## Local Overrides

Create `.claude/settings.local.json` for personal preferences:

```json
{
  "permissions": {
    "allow": [
      "Bash(your custom command:*)"
    ]
  }
}
```

Local settings override team settings for that specific configuration key.

## MCP Server

Loom provides a single **unified `mcp-loom` MCP server** (configured in
`.mcp.json`) that consolidates log monitoring, terminal management, and UI/state
control. It replaces the historical trio of separate `loom-logs` /
`loom-terminals` / `loom-ui` servers. Representative tools by category:

**Log tools**:
- `tail_daemon_log` - View daemon logs (`~/.loom/daemon.log`)
- `list_terminal_logs` - List terminal output files
- `tail_terminal_log` - View specific terminal output

**Terminal tools**:
- `list_terminals` - List all active terminals
- `get_terminal_output` - Read terminal output
- `get_selected_terminal` - Get current terminal info
- `send_terminal_input` - Execute commands in terminals
- `create_terminal` / `configure_terminal` / `restart_terminal` - Manage terminal sessions

**UI / state tools**:
- `read_console_log` - View browser console output (JavaScript errors, console.log statements)
- `read_state_file` - Read current application state (.loom/state.json)
- `read_config_file` - Read terminal configurations (.loom/config.json)
- `trigger_start` - Trigger workspace start with confirmation dialog
- `trigger_force_start` - Trigger force start without confirmation (immediate reset)

**Label State Machine Reset**: When the workspace is started (via `trigger_start` or `trigger_force_start`), the `reset_github_labels` daemon command automatically resets the GitHub label state machine:
- Removes `loom:building` from all open issues (workers can reclaim them)
- Replaces `loom:reviewing` with `loom:review-requested` on all open PRs (reviewer can re-review)
- This ensures a clean state when restarting the workspace with fresh agent terminals

See the mcp-loom package README for the full tool catalog.

**Note**: When you first open the project, Claude Code will prompt you to approve the MCP server. You can also enable it automatically by setting `"enableAllProjectMcpServers": true` in your `.claude/settings.local.json`.

## Slash Commands

The `commands/` directory contains slash commands that define Loom roles. Each command file contains the complete role definition - there's no indirection to separate role files.

### Available Commands

Commands under `.claude/commands/loom/` are invoked in the namespaced
`/loom:<role>` form (Claude Code 2.1+ requires this for subdirectory commands —
see #3345):

| Command | Role | Purpose |
|---------|------|---------|
| `/loom:builder` | Builder | Implements features for `loom:issue` issues and creates PRs |
| `/loom:judge` | Judge | Reviews PRs with `loom:review-requested` label |
| `/loom:curator` | Curator | Enhances issues and marks them as `loom:curated` |
| `/loom:architect` | Architect | Creates architectural proposals with `loom:architect` |
| `/loom:hermit` | Hermit | Identifies bloat and creates simplification issues |
| `/loom:doctor` | Doctor | Addresses PR feedback and resolves conflicts |
| `/loom:guide` | Guide | Triages issues and applies `loom:urgent` to top 3 |
| `/loom:champion` | Champion | Auto-merges approved PRs with `loom:pr` label |
| `/loom:help` | Help | Read-only overview of the installed `/loom:*` commands; `/loom:help <command>` describes one |
| `/loom:help <topic>` | Help | Comprehensive help guide with sub-topics (roles, workflow, commands, etc.) |

### How Slash Commands Work

**Manual Invocation**: Use slash commands to assume a role:
```bash
/loom:builder    # Assume Builder role, find and implement a loom:issue
/loom:judge      # Assume Judge role, review a PR with loom:review-requested
/loom:help       # Show comprehensive Loom help guide
```

Each slash command contains the complete role definition, including:
1. The role's purpose and responsibilities
2. Workflow guidelines and label transitions
3. Instructions for completing ONE iteration of the role's task

### Agent Roles in Workflow

The roles work together following the label-based workflow:

1. **architect** scans codebase → creates proposals with `loom:architect`
2. **User approves** → adds `loom:issue` label
3. **curator** enhances issues → marks as `loom:curated`
4. **User approves** → adds `loom:issue` label
5. **guide** prioritizes → adds `loom:urgent` to top 3
6. **builder** implements → creates PR with `loom:review-requested`
7. **judge** reviews → approves or requests changes
8. **doctor** fixes feedback → transitions back to `loom:review-requested`
9. **judge** approves → adds `loom:pr`
10. **champion** auto-merges the `loom:pr` PR → issue auto-closes via `Closes #N`

### Creating Custom Commands

To create a custom slash command:

1. Create `.claude/commands/your-command.md` (or `.claude/commands/your-namespace/command.md`) with the complete role definition
2. Include role purpose, workflow guidelines, and iteration instructions
3. Use it with `/your-command` (or `/your-namespace/command`)

**Note**: `.loom/roles/` contains symlinks to `.claude/commands/loom/` for backward compatibility. The single source of truth for all Loom role definitions is `.claude/commands/loom/`.

## Custom Subagents

The `agents/` directory contains custom subagent definitions for Loom roles. These subagents can be used with Claude Code's Task tool for spawning role-specific agents with fresh context.

### Available Subagents

| Subagent | Purpose |
|----------|---------|
| `loom-builder` | Implement features and fixes |
| `loom-judge` | Review pull requests |
| `loom-curator` | Enhance and organize issues |
| `loom-doctor` | Fix bugs and address PR feedback |
| `loom-champion` | Evaluate proposals, auto-merge PRs |
| `loom-architect` | Create architectural proposals |
| `loom-hermit` | Identify simplification opportunities |
| `loom-guide` | Prioritize and triage issues |
| `loom-auditor` | Validate main branch build/runtime |
| `loom-daemon` | Observe/dispatch the Rust `loom-daemon` via MCP tools |

The stubs do not carry a `model:` frontmatter field — a subagent's model is
resolved through the model-selection precedence chain (role JSON
`suggestedModel`, workspace override, or explicit dispatch param), not the stub.
See CLAUDE.md → "Model Selection Strategy".

> **Note**: the `loom-shepherd` subagent was removed in v0.10.0 along with the `/shepherd` slash command — see [the migration guide](https://github.com/rjwalters/loom/blob/main/docs/migration/v0.10.0-shepherd-deprecation.md). Use `/loom:sweep <issue>` (Tier 1) for the equivalent lifecycle. The `loom-daemon` subagent is preserved and now documents the Rust `loom-daemon` binary's MCP dispatch surface (`mcp__loom__dispatch_sweep` / `mcp__loom__list_sweeps` …) rather than the deleted Python brain.

### How Subagents Work

Subagents are specialized AI assistants that run in their own context window. Each has:
- Custom system prompt referencing the role definition in `.loom/roles/`
- Specific tool access appropriate for the role
- Model selection optimized for the task complexity

**Using Subagents with Task**:

The `/loom:sweep` orchestrator (or the Rust `loom-daemon`) can dispatch subagents for each phase. The recommended pattern is **native dispatch** -- pass the Loom role directly as `subagent_type`. Claude Code resolves `loom-<role>` against the `.claude/agents/loom-*.md` agent definitions that ship with Loom:

```python
# Spawn builder subagent with fresh context using native dispatch.
# subagent_type matches the agent definition name (loom-builder, loom-judge,
# loom-doctor, loom-curator, loom-champion, loom-architect, loom-hermit,
# loom-guide, loom-auditor).
result = Task(
    description="Builder phase for issue #123",
    prompt="Implement issue #123",
    subagent_type="loom-builder",
    run_in_background=False
)
```

The agent definition wires the correct system prompt and tool allowlist, so the caller only needs to supply the task-specific prompt (e.g., the issue number). The model is not set by the stub — it comes from the model-selection precedence chain (see CLAUDE.md → "Model Selection Strategy").

**Legacy pattern** (`subagent_type="general-purpose"` + a slash command in the prompt) still works for environments where the `loom-*` agent definitions are not installed, but prefer native dispatch when available:

```python
# Legacy fallback - role selection happens via the slash command in the prompt.
# Note: Claude Code 2.1+ requires the namespaced `/loom:<role>` form for
# subdirectory commands (`.claude/commands/loom/<role>.md`). See issue #3345.
result = Task(
    description="Builder phase for issue #123",
    prompt="/loom:builder 123",
    subagent_type="general-purpose",
    run_in_background=False
)
```

**Benefits**:
- **Fresh context**: Each subagent starts clean, avoiding context pollution
- **Role isolation**: Subagents focus on their specific task
- **Cost control**: Use faster/cheaper models for simpler roles (sonnet vs opus)
- **Better observability**: Clear which role is running

### Subagents vs Slash Commands

| Feature | Slash Commands | Subagents |
|---------|----------------|-----------|
| Context | Shared with main conversation | Isolated, fresh context |
| Invocation | `/loom:builder 123` | `Task(subagent_type="loom-builder", prompt="Implement issue #123")` (legacy: `subagent_type="general-purpose"` + `/loom:builder 123`) |
| Use case | Manual orchestration | Automated orchestration |
| Visibility | In main conversation | Spawned as separate task |

**Use slash commands** for manual orchestration mode where you want direct control.
**Use subagents** for automated orchestration where `/loom:sweep` (or `loom-daemon`) coordinates roles with fresh context per phase.

## Documentation

Full Claude Code settings documentation: https://docs.claude.com/en/docs/claude-code/settings
