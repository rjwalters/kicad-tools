# Loom Daemon - Iteration Mode (DEPRECATED)

**This file is deprecated.** The Loom daemon is now implemented in Python.

## Execution

Use the `/loom` skill or run directly:

```bash
./.loom/scripts/loom-daemon.sh {{ARGUMENTS}}
```

The Python daemon handles iteration logic internally in `loom_tools.daemon.iteration`.

## Migration

The two-tier LLM architecture (parent/iteration) has been replaced with a deterministic Python implementation:

- **Old**: `/loom iterate` -> `loom-iteration.md` with full gh commands
- **New**: `loom_tools.daemon.iteration.run_iteration()` in Python

Key changes:
- Snapshot capture via `build_snapshot()` (not LLM-interpreted gh commands)
- Completion checking via Python code (not Task() subagents)
- Shepherd spawning via `spawn_agent()` (not LLM-interpreted agent-spawn.sh)
- Deterministic action execution based on snapshot recommendations

See `loom-tools/src/loom_tools/daemon/` for the implementation.
