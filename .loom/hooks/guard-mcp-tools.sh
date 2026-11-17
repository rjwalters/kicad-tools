#!/usr/bin/env bash
# guard-mcp-tools.sh — PreToolUse guard for the `mcp__loom__*` tool namespace
# (issue #9108).
#
# THIN STUB. The decision logic is `loom-daemon guard-mcp-tools` (Rust,
# `loom-daemon/src/mcp_tool_guard.rs` + `loom-daemon/src/cli/guard_mcp_tools.rs`)
# — brand-new logic, so it went straight into the daemon per
# `.loom/docs/shell-language-policy.md` rather than starting as a script to be
# ported later (the `check-stale-blocked.sh` / `generate-agent-skills.sh`
# precedent). This file exists because a `PreToolUse` hook needs a
# shell-invocable command that `hook-wiring.sh` can resolve by name.
#
# WHAT IT GUARDS. Until #9108 the `PreToolUse` matchers were `Bash` (twice) and
# `Edit|Write` — nothing covered MCP tool calls, even though mcp-loom is
# registered at user scope and callable from every agent Loom spawns, and
# `get_agent_metrics` joined its raw arguments into a shell command line until
# #9107 replaced that with an `execFile` argv plus server-side allow-lists. This
# hook is the second, independent layer over that fix, not a substitute for it.
# The matcher this file is wired under is the namespace wildcard
# `mcp__loom__.*`, so a tool added to mcp-loom tomorrow is covered with no edit
# here. Two rules, both detailed in the Rust module's header and catalogued in
# `defaults/docs/guard-hooks.md`:
#
#   1. a shell metacharacter (`;`/`|`/`&`/`<`/`>`/backtick/newline/`$(`) in any
#      string argument, at any depth, outside a reviewed free-text field -> DENY;
#   2. a documented-enum argument off its schema's allow-list -> DENY.
#
# Toggle: guards.mcpToolArgs / LOOM_GUARD_MCP_TOOL_ARGS (default on).
#
# CONTRACT: same as every Loom guard — reads the PreToolUse JSON on stdin,
# prints a `hookSpecificOutput.permissionDecision` deny document or nothing, and
# NEVER exits non-zero. Every resolution failure here is an ALLOW (exit 0): a
# missing library, an unresolvable daemon binary, or a daemon below the declared
# floor. That is deliberate, and it is not the same question as a missing hook
# FILE — `hook-wiring.sh`'s rung 5 fails CLOSED on that, which is what makes
# failing open here safe rather than a silent hole.
#
# LOOM_SCRIPT_HELPER_MISSING_RC is 0 for exactly that reason: the version
# preflight below must land on 0, not on its usual 1.
#
# KNOWN ONE-VERSION WINDOW: the floor declared below is this repo's VERSION at
# the time the subcommand landed, because scripts/check-daemon-subcommand-versions.sh
# (correctly) refuses a floor above VERSION. A host running EXACTLY that version
# with a binary built from the parent commit therefore passes the preflight and
# reaches clap's own "unrecognized subcommand". Every older version is caught by
# the preflight and allows. `main` bumps VERSION on nearly every merge, so the
# window is one version wide; roll the host (`loom update`) to close it.

set -uo pipefail  # NOTE: no -e — a PreToolUse guard must never exit non-zero

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"

# Workspace root, resolved the way every other Loom hook resolves it:
# LOOM_PROJECT_ROOT (set by the settings.json wrapper and by hook-wiring.sh)
# wins, then git-common-dir/.. (the MAIN checkout even from a linked worktree),
# then Claude Code's project dir, then the cwd.
ROOT="${LOOM_PROJECT_ROOT:-}"
if [[ -z "$ROOT" ]]; then
    GIT_COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null)" || GIT_COMMON_DIR=""
    [[ -n "$GIT_COMMON_DIR" ]] && ROOT="$(cd "$GIT_COMMON_DIR/.." 2>/dev/null && pwd)"
fi
[[ -n "$ROOT" ]] || ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"

# At runtime SCRIPT_DIR is .loom/hooks/ (project-level wiring) or
# defaults/hooks/ (machine-level wiring, #4262); in both layouts ../scripts/lib
# resolves — the same relative source guard-worktree-paths.sh uses.
LIB="$SCRIPT_DIR/../scripts/lib/locate-daemon-bin.sh"
[[ -r "$LIB" ]] || exit 0
# shellcheck source=../scripts/lib/locate-daemon-bin.sh
source "$LIB" 2>/dev/null || exit 0

# shellcheck disable=SC2034  # read by loom_daemon_version_preflight, sourced above
LOOM_SCRIPT_HELPER_MISSING_RC=0

# $LOOM_DAEMON_SELF_BIN ("the binary that IMPLEMENTS this stub") first, then the
# ordinary chain — the same precedence skip-labels.sh and premise-check.sh apply.
BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$ROOT")"
[[ -n "$BIN" ]] || exit 0

# requires-daemon: guard-mcp-tools >= 0.19.502   #9108 — the subcommand itself; below this floor the preflight refuses with the roll command and exits 0 (allow), instead of letting clap's "unrecognized subcommand" exit 2, which a PreToolUse hook reports as a BLOCK with an unusable reason.
loom_daemon_version_preflight guard-mcp-tools "$BIN"
exec "$BIN" guard-mcp-tools --repo-root "$ROOT"
