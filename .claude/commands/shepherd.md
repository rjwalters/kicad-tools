# Shepherd

Orchestrate issue lifecycle via the shell-based shepherd script.

## Arguments

**Arguments**: $ARGUMENTS

Parse the issue number and any flags from the arguments.

## Supported Options

| Flag | Description |
|------|-------------|
| `--force` or `-f` | Auto-approve, resolve conflicts, auto-merge after approval |
| `--to <phase>` | Stop after specified phase (curated, pr, approved) |
| `--task-id <id>` | Continue from previous checkpoint |

**Deprecated options** (still work with deprecation warnings):
- `--force-pr` - Now the default behavior
- `--force-merge` - Use `--force` or `-f` instead
- `--wait` - No longer blocks; shepherd always exits after PR approval

## Examples

```bash
/shepherd 123                    # Exit after PR approval (default)
/shepherd 123 --force            # Fully automated, auto-merge after review
/shepherd 123 -f                 # Same as above (short form)
/shepherd 123 --to curated       # Stop after curation phase
```

## Execution

Invoke the shell script with all provided arguments:

```bash
./.loom/scripts/shepherd-loop.sh $ARGUMENTS
```

Run this command now. Report the exit status when complete.

## Reference Documentation

For detailed orchestration workflow, phase definitions, and troubleshooting:
- **Lifecycle details**: `.claude/commands/shepherd-lifecycle.md`
- **Shell script**: `.loom/scripts/shepherd-loop.sh`
