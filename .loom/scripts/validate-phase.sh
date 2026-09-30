#!/bin/bash

# validate-phase.sh - Thin stub over `loom-daemon validate-phase`.
#
# Usage:
#   validate-phase.sh <phase> <issue-number> [options]
#
# The real implementation is native (loom-daemon/src/script_helpers/validate_phase.rs),
# ported from `loom_tools.validate_phase` in issue #4275 (epic #4081 Phase 3
# family 5). Arguments and exit codes are unchanged.
#
# Exit codes:
#   0 - Contract satisfied (initially or after recovery)
#   1 - Contract failed, recovery failed or not possible
#   2 - Invalid arguments

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# requires-daemon: validate-phase >= 0.17.0   #4275/#4552 — the Rust port landed in 3f147e8f0 (2026-07-29), AFTER v0.16.0 was tagged (57cfec3aa) and while the workspace version still read 0.16.0, so 0.16.0 is the last version WITHOUT it; the next bump, 0.17.0 (3e5a9439e), is the first that shipped it. Hard, not `optional`: this stub only execs, it never probes or degrades (#8484).
#
# LOOM_SCRIPT_HELPER_MISSING_RC=2, NOT the default 1: unlike the other five
# #4552 stubs, exit 1 here is an ANSWER — "contract failed, recovery failed or
# not possible", the verdict a caller acts on — while 2 is this entry point's
# own "could not run" code (invalid arguments). A version refusal wearing 1
# would be read as a failed phase contract and trigger recovery/abort on a host
# whose only real problem is a stale binary. Set as a command prefix rather
# than a standalone assignment so this `settled` file gains no code line
# (scripts/check-shell-allowlist.sh: a settled script may shrink, never grow).
LOOM_SCRIPT_HELPER_MISSING_RC=2 loom_exec_script_helper validate-phase "$@"
