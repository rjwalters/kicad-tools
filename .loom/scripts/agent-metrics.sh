#!/bin/bash

# agent-metrics.sh - Thin stub delegating to `loom-daemon stats <command>`
#
# This script preserves the CLI interface for backwards compatibility.
# The MCP tool (mcp-loom) shells out to this script, so the interface
# must remain stable.
#
# Usage:
#   agent-metrics.sh [--role ROLE] [--period PERIOD] [--format FORMAT]
#   agent-metrics.sh summary
#   agent-metrics.sh effectiveness [--role ROLE] [--by-model]
#   agent-metrics.sh costs [--issue NUMBER] [--by-model]
#   agent-metrics.sh velocity
#   agent-metrics.sh --help
#
# --by-model (#3482, Phase 3a) adds a per-model dimension to the
# effectiveness/costs output; NULL/absent model values render as 'default'.
#
# The Python `loom_tools.agent_metrics` implementation was ported to native
# `loom-daemon stats <command>` in epic #4081 Phase 3 family 4 (issue #4274);
# this stub now execs the daemon binary instead of the Python CLI. All flags
# and the positional command word forward unchanged.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
# shellcheck source=lib/script-helper.sh
source "$SCRIPT_DIR/lib/script-helper.sh"

# --model-experiment (or `sweep-experiment`) routes to the sweep model-cost
# experiment reader (#3725) rather than the activity-db metrics. It aggregates
# .loom/stats/sweep-model-stats.jsonl into the per-arm inequality inputs #3718
# needs; keeping it here puts it next to the existing --by-model cost dimension.
# Issue #4275 (epic #4081 Phase 3 family 5) ported `loom_tools.sweep_experiment`
# to native `loom-daemon sweep-experiment`, so this branch now execs the daemon
# subcommand instead of the Python module; the forwarded flags are unchanged.
if [[ "${1:-}" == "--model-experiment" || "${1:-}" == "sweep-experiment" ]]; then
    shift
    # requires-daemon: sweep-experiment >= 0.17.0   #4275/#4552 — the Rust port landed in 3f147e8f0 (2026-07-29), AFTER v0.16.0 was tagged (57cfec3aa) and while the workspace version still read 0.16.0, so 0.16.0 is the last version WITHOUT it; the next bump, 0.17.0 (3e5a9439e), is the first that shipped it. This declares the `--model-experiment` branch only — the `stats` dependency below is a separate, still-grandfathered pair. Hard, not `optional`: the branch only execs, it never probes or degrades. Default LOOM_SCRIPT_HELPER_MISSING_RC (1) is left alone deliberately — `sweep-experiment harvest` exits 0 or propagates an error and has no data exit code, so the refusal cannot be mistaken for an answer (#8484).
    loom_exec_script_helper sweep-experiment harvest "$@"
fi

REPO_ROOT="$(_lsh_find_repo_root "$SCRIPT_DIR" 2>/dev/null || echo "$PWD")"
DAEMON_BIN="$(loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -z "$DAEMON_BIN" ]]; then
    echo "[ERROR] loom-daemon binary not found." >&2
    echo "" >&2
    echo "Build it with: cd loom-daemon && cargo build --release" >&2
    echo "Or set LOOM_DAEMON_BIN to an existing binary." >&2
    exit 1
fi

# `loom-daemon stats` with NO positional command runs its original
# interactive dashboard (backward compatible for direct CLI use) — but the
# Python CLI this script used to delegate to defaulted the positional
# command to "summary" (`argparse(..., nargs="?", default="summary")`), and
# the MCP `get_agent_metrics` tool relies on that default (it only forwards
# an explicit command word when it differs from "summary"). Preserve that
# contract here: prepend "summary" whenever the caller didn't supply one of
# the four agent-metrics command words as the first argument.
case "${1:-}" in
    summary|effectiveness|costs|velocity)
        exec "$DAEMON_BIN" stats "$@"
        ;;
    *)
        exec "$DAEMON_BIN" stats summary "$@"
        ;;
esac
