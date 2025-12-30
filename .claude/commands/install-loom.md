# Install Loom into Target Repository

Orchestrate the complete Loom installation workflow including GitHub issue creation, worktree setup, label sync, and PR creation.

## Prerequisites

This command should be launched via the `install-loom.sh` wrapper script, which sets required environment variables:

```bash
cd /path/to/loom
./scripts/install-loom.sh /path/to/target-repo
```

**Required Environment Variables**:
- `LOOM_VERSION` - Loom version from package.json
- `LOOM_COMMIT` - Short commit hash
- `LOOM_ROOT` - Path to Loom repository
- `TARGET_PATH` - Path to target repository

## Installation Workflow

Follow these steps in order to complete the installation:

### Step 1: Validate Target Repository

Run the validation script to ensure prerequisites are met:

```bash
cd "$TARGET_PATH"
"$LOOM_ROOT/scripts/install/validate-target.sh" "$TARGET_PATH"
```

**Verification**:
- Target is a valid git repository
- GitHub CLI (gh) is installed and authenticated
- Repository remote is accessible

**If validation fails**: Stop and report the error to the user. Do not proceed.

### Step 2: Create Tracking Issue

Create a GitHub issue in the target repository to track the installation:

```bash
cd "$TARGET_PATH"
ISSUE_NUMBER=$("$LOOM_ROOT/scripts/install/create-issue.sh" "$TARGET_PATH")
echo "Created issue #$ISSUE_NUMBER"
```

**Verification**:
- Issue number is captured (numeric value)
- Issue is created with `loom:building` label
- Issue body includes Loom version and commit information

### Step 3: Create Installation Worktree

Create a git worktree in the target repository for the installation work:

```bash
cd "$TARGET_PATH"
WORKTREE_PATH=$("$LOOM_ROOT/scripts/install/create-worktree.sh" "$TARGET_PATH" "$ISSUE_NUMBER")
echo "Created worktree: $WORKTREE_PATH"
```

**Verification**:
- Worktree created at `.loom/worktrees/issue-{NUMBER}`
- Branch created: `feature/loom-installation`
- Working directory switched to worktree

### Step 4: Initialize Loom Configuration

Run `loom-daemon init` to install Loom files in the worktree:

```bash
cd "$TARGET_PATH/$WORKTREE_PATH"
"$LOOM_ROOT/target/release/loom-daemon" init .
```

**If loom-daemon binary doesn't exist**:
```bash
cd "$LOOM_ROOT"
pnpm daemon:build
```

**Verification**:
- `.loom/` directory created with configuration
- `.claude/` directory created (target-specific MCP servers only)
- `.github/` directory created with labels and workflows
- `CLAUDE.md` and `AGENTS.md` files created/updated
- `.gitignore` updated with Loom patterns

### Step 5: Sync GitHub Labels

Sync Loom workflow labels to the target repository:

```bash
cd "$TARGET_PATH/$WORKTREE_PATH"
"$LOOM_ROOT/scripts/install/sync-labels.sh" .
```

**Verification**:
- Labels from `.github/labels.yml` synced to repository
- Existing labels preserved (non-destructive)
- New Loom labels created

### Step 6: Create Pull Request

Commit all changes and create a pull request:

```bash
cd "$TARGET_PATH/$WORKTREE_PATH"
"$LOOM_ROOT/scripts/install/create-pr.sh" . "$ISSUE_NUMBER"
```

**Verification**:
- All changes committed with descriptive message
- Branch pushed to origin
- PR created with `loom:review-requested` label
- PR body includes installation summary and next steps
- PR closes the tracking issue

## Success Criteria

After completing all steps, verify:

- ✅ Tracking issue created in target repository
- ✅ Installation worktree created at `.loom/worktrees/issue-{NUMBER}`
- ✅ All Loom files installed (`.loom/`, `.claude/`, `.github/`, documentation)
- ✅ GitHub labels synced
- ✅ Pull request created and linked to issue
- ✅ PR ready for human review and merge

## Reporting Results

After completing the installation, report to the user:

```
✓ Loom Installation Complete

📋 Tracking Issue: #{ISSUE_NUMBER}
📦 Installation PR: {PR_URL}

## What's Included:
- ✅ .loom/ directory with configuration and scripts
- ✅ .claude/ directory with MCP servers and prompts
- ✅ .github/ directory with labels and workflows
- ✅ CLAUDE.md and AGENTS.md documentation

## Next Steps:
1. Review the pull request: {PR_URL}
2. Merge when ready
3. Choose your workflow mode:
   - **Tauri App Mode**: Open repository in Loom app
   - **MOM Mode**: Use Claude Code terminals with role-based workflows

See .loom/CLAUDE.md in the target repository for complete usage details.
```

## Error Handling

If any step fails:

1. **Report the specific error** with full context
2. **Show the failed command** and its output
3. **Provide recovery suggestions**:
   - Validation failures: Fix prerequisites (install gh CLI, authenticate, etc.)
   - Issue creation failures: Check GitHub permissions
   - Worktree failures: Clean up existing worktrees with `git worktree prune`
   - Init failures: Ensure loom-daemon is built (`pnpm daemon:build`)
   - Label sync failures: Non-critical, can be done manually later
   - PR creation failures: Check for uncommitted changes, branch conflicts

4. **Cleanup on failure** (if applicable):
   ```bash
   # If issue was created but later steps failed
   gh issue close $ISSUE_NUMBER --comment "Installation failed, cleaning up"

   # If worktree was created but later steps failed
   cd "$TARGET_PATH"
   git worktree remove "$WORKTREE_PATH" --force
   ```

## Notes

- This command is designed to be **idempotent** where possible
- Each script handles its own error checking and reporting
- Scripts are **modular** and can be run independently for debugging
- The installation is **non-destructive** - existing files are preserved
- Documentation files (CLAUDE.md, AGENTS.md) are **appended to** if they exist

## Testing the Installation

After PR is merged, verify the installation works:

```bash
# Test in Tauri App Mode
# 1. Open Loom app
# 2. Select the target repository as workspace
# 3. Start engine - should create terminals with roles

# Test in MOM Mode
# 1. Open Claude Code in target repository
# 2. Use slash commands: /builder, /judge, /curator, /champion, etc.
# 3. Verify MCP servers work (if target-specific ones were added)
```

## Related Documentation

After installation, users should refer to:
- `.loom/CLAUDE.md` - Comprehensive Loom usage guide
- `.loom/AGENTS.md` - Agent workflow and role details
- `.loom/roles/*.md` - Individual role definitions
