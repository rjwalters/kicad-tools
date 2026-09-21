# Agent Pool CLI (`.loom/bin/loom`)

Full command reference for `.loom/bin/loom`, the tmux-based background agent
pool (spawns agents from `.loom/config.json`). Referenced from `.loom/CLAUDE.md`
→ "Usage Modes" → "Agent Pool".

| Command | Purpose |
|---------|---------|
| `./.loom/bin/loom start` | Start all configured agents (`--only <role>` to filter, `--dry-run` to preview) |
| `./.loom/bin/loom status` | List running `loom-*` tmux sessions with id / name / role, plus any configured agent that is not running (`--json` for machine-readable output) |
| `./.loom/bin/loom stop` | Graceful shutdown (`--force` to kill immediately, `<agent>` to stop one) |
| `./.loom/bin/loom attach <id>` | Attach to a running agent's tmux session |
| `./.loom/bin/loom logs <id>` | Tail an agent's output |
| `./.loom/bin/loom health` / `scale <role> <n>` | Diagnostic daemon health check / dynamic agent scaling |
| `./.loom/bin/loom help` | Full command list; `./.loom/bin/loom <cmd> --help` for per-command help |

The legacy `./loom.sh` wrapper (and `.loom/scripts/start-daemon.sh` /
`stop-daemon.sh`) are thin shims that now delegate to these `.loom/bin/loom`
subcommands, kept only for backwards compatibility.

**Safe to run from inside a Claude Code session.** `.loom/bin/loom start` spawns
each agent with `tmux new-session -d` on a dedicated `-L loom` socket, so an
agent started from within a Claude Code session is never a descendant of that
session and survives its exit.

> For single-issue lifecycle orchestration prefer `/loom:sweep <issue>` (Tier 1),
> and for multi-account autonomous dispatch use the Rust `loom-daemon` binary via
> `mcp__loom__dispatch_sweep` (Tier 2).
