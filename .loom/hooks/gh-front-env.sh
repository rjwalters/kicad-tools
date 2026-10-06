#!/usr/bin/env bash
# gh-front-env.sh — SessionStart hook: put the agent `gh` front first on the
# session's PATH (issue #10516).
#
# THIN STUB. The logic is `loom-daemon gh-shim session-env`
# (`loom-daemon/src/agent_gh/session_env.rs`): it appends one guarded PATH line
# to `$CLAUDE_ENV_FILE`, which Claude Code sources before every Bash tool call
# in the session AND in its Task subagents, so their plain `gh` reads are
# ETag-revalidated like a dispatched worker's (#10331). Order is the worker's:
# managed launcher (#9987) when a policy names one, then the front, then the
# PATH the session already had. Opt out with LOOM_GH_SHIM=0.
#
# CONTRACT: a cost optimisation, never a guard. Every resolution failure is
# exit 0, `session-env` itself always exits 0, and nothing reaches stdout
# (SessionStart stdout is injected into the model's context). Diagnostics go
# to stderr. The workspace (LOOM_PROJECT_ROOT from the hook wrapper, else the
# cwd's main checkout) is resolved and gated by the subcommand itself.

set -uo pipefail  # NOTE: no -e — this hook must never fail a session

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"

LIB="$SCRIPT_DIR/../scripts/lib/locate-daemon-bin.sh"
[[ -r "$LIB" ]] || exit 0
# shellcheck source=../scripts/lib/locate-daemon-bin.sh
source "$LIB" >/dev/null 2>&1 || exit 0
# shellcheck disable=SC2034  # read by loom_daemon_version_preflight, sourced above
LOOM_SCRIPT_HELPER_MISSING_RC=0
BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "${LOOM_PROJECT_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}}")" 2>/dev/null
[[ -n "$BIN" && -x "$BIN" ]] || exit 0

# requires-daemon: gh-shim >= 0.19.772   #10516 — `gh-shim session-env`; below this floor the preflight explains on stderr and exits 0, so the session keeps its PATH. (One-version window: a build of exactly this VERSION from the parent commit prints gh-shim usage and exits 2, which SessionStart reports to the user without blocking.)
loom_daemon_version_preflight gh-shim "$BIN"
exec "$BIN" gh-shim session-env 1>&2
