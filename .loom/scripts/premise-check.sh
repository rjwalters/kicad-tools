#!/usr/bin/env bash
# premise-check.sh - the premise gate, one stage BEFORE Curator (issue #8396).
#
# THIN STUB. The implementation is `loom-daemon premise-check` (Rust,
# `loom-daemon/src/premise_check/`); new logic goes there, per
# `.loom/docs/shell-language-policy.md`. This entry point exists because role
# prompts (`curator.md`) and the sweep orchestrator
# (`sweep-wave-lifecycle.md`) invoke it BY PATH and branch on its exit code.
#
# WHAT IT ANSWERS
#
# "Should this issue be enriched at all, or does changing the behaviour it
# reports need a human ruling first?" Full contract, the record format, and
# the design rationale: `.loom/docs/premise-gate.md`.
#
# Usage:
#   premise-check.sh --issue <N> [--repo <owner/repo>] [--no-scan]
#   premise-check.sh --body-file <path> [--title <t>] [--labels <a,b>] \
#                    [--record-file <path>] [--repo-root <path>]
#
# Exit codes (contract):
#   0   PROCEED          out of scope, or a consistent verdict=clear record
#   10  RECORD-REQUIRED  in scope, no record - do the check BEFORE enriching
#   11  ROUTE-OPERATOR   loom:operator-only + loom:operator-decision, no pass
#   12  RECORD-MALFORMED the record fails a structural rule (treat as 10)
#   13  PREMISE-FALSE    the behaviour does not exist as described
#   1   ERROR            FAILS CLOSED - treat as 10, never as 0
#
# `exec` is load-bearing twice over: the subcommand's own exit code reaches the
# caller unmodified (every code above is data a caller branches on, so a
# wrapper that remapped any of them would change what a Curator does), and no
# shell intermediary is left behind to forward signals.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd)" || REPO_ROOT="$PWD"

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

# LOOM_DAEMON_SELF_BIN ("the binary that IMPLEMENTS this stub") first, then the
# ordinary chain - the same precedence lib/script-helper.sh applies, and the
# seam the test harness pins (#8134).
DAEMON_BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -z "$DAEMON_BIN" ]]; then
    echo "[ERROR] loom-daemon not found (needed for 'premise-check')." >&2
    echo "  Build it:  cargo build --release --package loom-daemon" >&2
    echo "  Or set:    LOOM_DAEMON_SELF_BIN=/path/to/loom-daemon" >&2
    echo "  Exiting 1: the gate FAILS CLOSED - treat this as exit 10." >&2
    exit 1
fi

# requires-daemon: premise-check >= 0.19.238   #8396 — the premise-check gate; without it a resolved binary predating this subcommand refuses with clap's "unrecognized subcommand" (this thin stub itself fails closed above, exit 1, when no binary resolves at all)
exec "$DAEMON_BIN" premise-check "$@"
