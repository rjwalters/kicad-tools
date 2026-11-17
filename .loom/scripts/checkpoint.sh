#!/bin/bash

# checkpoint.sh - Manage builder checkpoints for progress tracking
#
# This script allows builders to write checkpoints as they progress through
# stages of work. The shepherd uses these checkpoints to make smarter recovery
# decisions when builders fail.
#
# Usage:
#   checkpoint.sh write --stage <stage> [options]
#   checkpoint.sh read [--json]
#   checkpoint.sh clear
#   checkpoint.sh stages
#
# Stages (in order of progression):
#   planning      - Reading issue, planning approach
#   implementing  - Writing code, making changes
#   tested        - Tests ran (pass or fail)
#   committed     - Changes committed locally
#   pushed        - Branch pushed to remote
#   pr_created    - PR exists with proper labels
#
# Examples:
#   # Write checkpoint when starting implementation
#   checkpoint.sh write --stage implementing --issue 42
#
#   # Write checkpoint after tests pass
#   checkpoint.sh write --stage tested --test-result pass --test-command "pnpm check:ci"
#
#   # Write checkpoint after commit
#   checkpoint.sh write --stage committed --commit-sha abc123
#
#   # Read current checkpoint
#   checkpoint.sh read
#
#   # Read checkpoint as JSON
#   checkpoint.sh read --json
#
# See `loom-daemon checkpoint --help` for full usage. This is a thin stub over
# that native subcommand (issue #4275, epic #4081 Phase 3 family 5 — the native
# port of the retired `loom_tools.checkpoints`); commands and flags are
# unchanged.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# requires-daemon: checkpoint >= 0.17.0   #4275/#4552 — the Rust port landed in 3f147e8f0 (2026-07-29), AFTER v0.16.0 was tagged (57cfec3aa) and while the workspace version still read 0.16.0, so 0.16.0 is the last version WITHOUT it; the next bump, 0.17.0 (3e5a9439e), is the first that shipped it. Hard, not `optional`: this stub only execs, it never probes or degrades. Default LOOM_SCRIPT_HELPER_MISSING_RC (1) is left alone deliberately — `loom-daemon checkpoint` uses 1 only for "the write/clear failed", an ERROR, never data a caller branches on (`read` and `stages` always exit 0), so the refusal cannot be mistaken for an answer (#8484).
loom_exec_script_helper checkpoint "$@"
