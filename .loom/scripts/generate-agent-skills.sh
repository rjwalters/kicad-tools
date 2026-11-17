#!/usr/bin/env bash
# generate-agent-skills.sh - generate .agents/skills/loom-<name>/SKILL.md from
# role prompts (issue #8673, contract point 5).
#
# THIN STUB. The implementation is `loom-daemon generate-agent-skills` (Rust,
# `loom-daemon/src/agent_skills.rs`); new logic goes there, per
# `.loom/docs/shell-language-policy.md` — this is brand-new logic, so it went
# straight into the daemon rather than starting as a script to be ported
# later. This entry point exists so CI and an operator can invoke a stable
# path without knowing the daemon subcommand name.
#
# WHAT IT DOES: generates `defaults/.agents/skills/loom-<name>/SKILL.md` for
# every `defaults/roles/<name>.md` role prompt — the cross-vendor
# skill-discovery surface Codex, Kimi Code, Mistral Vibe, and Grok read
# natively (`runtime-adapters.md` §5). Each file carries a
# `<!-- loom-managed-skill -->` marker install/resync tooling gates overwrites
# on.
#
# Usage:
#   generate-agent-skills.sh          Write every generated SKILL.md
#   generate-agent-skills.sh --check  Exit non-zero if any file is missing or stale; write nothing
#   generate-agent-skills.sh --list   Print "loom-<name> -> <path>"; write nothing
#
# Exit codes: forwarded unchanged from `loom-daemon generate-agent-skills`.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || pwd)"
# $LOOM_DAEMON_SELF_BIN ("the binary that IMPLEMENTS this stub") first, then the
# ordinary chain — the same precedence lib/script-helper.sh, premise-check.sh,
# and skip-labels.sh apply (#8134).
BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -z "$BIN" ]]; then
    echo "[ERROR] loom-daemon not found (needed for 'generate-agent-skills')." >&2
    echo "Build it: cargo build --release --manifest-path loom-daemon/Cargo.toml" >&2
    echo "Or set LOOM_DAEMON_SELF_BIN / LOOM_DAEMON_BIN to an existing binary." >&2
    exit 1
fi

# requires-daemon: generate-agent-skills >= 0.19.297   #8673 — the subcommand itself; below this floor a resolved binary refuses with clap's "unrecognized subcommand" instead of the version-floor message the preflight below gives.
loom_daemon_version_preflight generate-agent-skills "$BIN"
exec "$BIN" generate-agent-skills "$@"
