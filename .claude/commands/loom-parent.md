# Loom Daemon - Parent Mode (DEPRECATED)

**This file is deprecated.** The Loom daemon is now implemented in Python.

## Execution

Use the `/loom` skill or run directly:

```bash
./.loom/scripts/loom-daemon.sh {{ARGUMENTS}}
```

The Python daemon handles all orchestration internally:
- Main event loop
- Iteration logic
- Shepherd spawning
- Support role management
- State management

## Migration

The two-tier LLM architecture (parent/iteration) has been replaced with a deterministic Python implementation:

- **Old**: `/loom` -> `loom-parent.md` -> Task() -> `loom-iteration.md`
- **New**: `/loom` -> `loom-daemon.sh` -> `loom-daemon` (Python CLI)

See `loom-tools/src/loom_tools/daemon/` for the implementation.
