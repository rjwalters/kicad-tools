# Claude Code Settings

This directory contains Claude Code configuration for the Loom project.

## Files

- **`settings.json`**: Team-wide permissions and settings (committed to git)
- **`settings.local.json`**: Personal preferences (gitignored, create if needed)
- **`../.mcp.json`**: MCP server configuration (at project root, committed to git)

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
- `pnpm install` - Install dependencies
- `pnpm build` - Build project
- `pnpm tauri:dev` - Run Tauri dev server
- `pnpm daemon:dev` - Run daemon in dev mode
- `pnpm check:all` - Run all checks
- `pnpm check:ci` - Run CI checks locally

### Code Quality
- `pnpm lint` - Biome linting
- `pnpm format` - Biome formatting
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

## MCP Servers

The project includes two MCP servers configured in `.mcp.json`:

### loom-logs
Monitor Loom application logs:
- `tail_daemon_log` - View daemon logs (`~/.loom/daemon.log`)
- `tail_tauri_log` - View Tauri app logs (`~/.loom/tauri.log`)
- `list_terminal_logs` - List terminal output files
- `tail_terminal_log` - View specific terminal output

### loom-terminals
Interact with Loom terminal sessions:
- `list_terminals` - List all active terminals
- `get_terminal_output` - Read terminal output
- `get_selected_terminal` - Get current terminal info
- `send_terminal_input` - Execute commands in terminals

### loom-ui
Interact with the Loom application UI and state:
- `read_console_log` - View browser console output (JavaScript errors, console.log statements)
- `read_state_file` - Read current application state (.loom/state.json)
- `read_config_file` - Read terminal configurations (.loom/config.json)
- `trigger_start` - Trigger workspace start with confirmation dialog (factory reset with 6 terminals)
- `trigger_force_start` - Trigger force start without confirmation (immediate reset)

**Label State Machine Reset**: When workspace is started (via `trigger_start` or `trigger_force_start`), the `reset_github_labels` Tauri command automatically resets the GitHub label state machine:
- Removes `loom:building` from all open issues (workers can reclaim them)
- Replaces `loom:reviewing` with `loom:review-requested` on all open PRs (reviewer can re-review)
- This ensures a clean state when restarting the workspace with fresh agent terminals

**Note**: When you first open the project, Claude Code will prompt you to approve these MCP servers. You can also enable them automatically by setting `"enableAllProjectMcpServers": true` in your `.claude/settings.local.json`.

## Slash Commands

The `commands/` directory contains slash commands that invoke Loom roles. Each command instructs Claude to assume a specific role defined in `.loom/roles/`.

### Available Commands

| Command | Role | Purpose |
|---------|------|---------|
| `/builder` | Builder | Implements features for `loom:issue` issues and creates PRs |
| `/judge` | Judge | Reviews PRs with `loom:review-requested` label |
| `/curator` | Curator | Enhances issues and marks them as `loom:curated` |
| `/architect` | Architect | Creates architectural proposals with `loom:architect` |
| `/hermit` | Hermit | Identifies bloat and creates simplification issues |
| `/doctor` | Doctor | Addresses PR feedback and resolves conflicts |
| `/guide` | Guide | Triages issues and applies `loom:urgent` to top 3 |
| `/champion` | Champion | Auto-merges approved PRs with `loom:pr` label |

### How Slash Commands Work

**Manual Invocation**: Use slash commands to assume a role:
```bash
/builder    # Assume Builder role, find and implement a loom:issue
/judge      # Assume Judge role, review a PR with loom:review-requested
```

Each slash command tells Claude to:
1. Read the role definition from `.loom/roles/<role>.md`
2. Follow the role's workflow guidelines
3. Complete ONE iteration of the role's task

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
9. **User merges** → issue auto-closes

### Creating Custom Commands

To create a custom slash command:

1. Create `.claude/commands/your-command.md`
2. Reference a role file or write custom instructions
3. Use it with `/your-command`

See `.loom/roles/README.md` for creating custom role definitions.

## Documentation

Full Claude Code settings documentation: https://docs.claude.com/en/docs/claude-code/settings
