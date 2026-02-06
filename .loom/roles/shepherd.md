# Shepherd

Orchestrate issue lifecycle via the shell-based shepherd script.

## Arguments

**Arguments**: $ARGUMENTS

Parse the issue number and any flags from the arguments.

## Supported Options

| Flag | Description |
|------|-------------|
| `--merge` or `-m` | Auto-approve, resolve conflicts, auto-merge after approval. Also overrides `loom:blocked` status. |
| `--to <phase>` | Stop after specified phase (curated, pr, approved) |
| `--task-id <id>` | Continue from previous checkpoint |

**Deprecated options** (still work with deprecation warnings):
- `--force` or `-f` - Use `--merge` or `-m` instead
- `--force-pr` - Now the default behavior
- `--force-merge` - Use `--merge` or `-m` instead
- `--wait` - No longer blocks; shepherd always exits after PR approval

## Examples

```bash
/shepherd 123                    # Exit after PR approval (default)
/shepherd 123 --merge            # Fully automated, auto-merge after review
/shepherd 123 -m                 # Same as above (short form)
/shepherd 123 --to curated       # Stop after curation phase
```

## Execution

Invoke the shepherd wrapper with all provided arguments:

```bash
./.loom/scripts/loom-shepherd.sh $ARGUMENTS
```

Run this command now. Report the exit status when complete.

## Reference Documentation

For detailed orchestration workflow, phase definitions, and troubleshooting:
- **Lifecycle details**: `.claude/commands/shepherd-lifecycle.md`
- **Wrapper script**: `.loom/scripts/loom-shepherd.sh` (routes to Python)
- **Python implementation**: `loom-tools/src/loom_tools/shepherd/`
