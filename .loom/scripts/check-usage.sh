#!/bin/bash
# check-usage.sh - Query Claude API usage via Anthropic OAuth API
#
# Usage:
#   ./.loom/scripts/check-usage.sh           # Returns JSON with usage data
#   ./.loom/scripts/check-usage.sh --status  # Human-readable status
#
# Exit codes:
#   0 - Data returned successfully
#   1 - Token not found or API call failed
#
# Thin stub over the native `loom-daemon usage` subcommand (issue #4275, epic
# #4081 Phase 3 family 5 — the native port of the retired
# `loom_tools.common.usage`). Reads the Claude Code OAuth token from the macOS
# Keychain and calls the Anthropic usage API, caching to
# `.loom/usage-cache.json`. Flags and exit codes are unchanged.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# requires-daemon: usage >= 0.17.0   #4275/#4552 — the Rust port landed in 3f147e8f0 (2026-07-29), AFTER v0.16.0 was tagged (57cfec3aa) and while the workspace version still read 0.16.0, so 0.16.0 is the last version WITHOUT it; the next bump, 0.17.0 (3e5a9439e), is the first that shipped it. Hard, not `optional`: this stub only execs, it never probes or degrades. Default LOOM_SCRIPT_HELPER_MISSING_RC (1) is left alone deliberately — `loom-daemon usage` uses 1 only for "no Keychain token / API failure", an ERROR, never data a caller branches on, so the refusal cannot be mistaken for an answer (#8484).
loom_exec_script_helper usage "$@"
