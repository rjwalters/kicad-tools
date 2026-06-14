#!/usr/bin/env bash
# Soft-deprecation warning helper (issue #3376, epic #3372).
#
# Phase 2b of the shepherd/daemon deprecation epic. Source this file from
# any shell entry point that wraps a deprecated component and call
# warn_deprecated to print a one-shot stderr warning. Set
# LOOM_SUPPRESS_DEPRECATION=1 in the environment to silence it.
#
# This file must be safe to source (no side effects at source time — only
# function definitions). Calling warn_deprecated multiple times in a single
# script is allowed; each call emits its own warning.
#
# Usage:
#   # shellcheck source=.loom/scripts/lib/deprecation.sh
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/deprecation.sh"
#   warn_deprecated "loom-daemon" "./.loom/scripts/spawn-loop.sh + GH Actions"

# warn_deprecated <component> <replacement> [ref]
#
# Emits a multi-line ⚠️ DEPRECATED block to stderr. Returns 0 even when
# suppressed; never errors. The third argument defaults to "#3372" (the
# umbrella epic).
warn_deprecated() {
    # Respect the global suppression env var. Treat unset as "not suppressed".
    if [[ "${LOOM_SUPPRESS_DEPRECATION:-}" == "1" ]]; then
        return 0
    fi

    local component="${1:-<unspecified>}"
    local replacement="${2:-<see #3372>}"
    local ref="${3:-#3372}"

    cat >&2 <<EOF
⚠️  DEPRECATED: ${component} is scheduled for removal in the next major release.
    Replacement: ${replacement}
    See ${ref}.
    Suppress with LOOM_SUPPRESS_DEPRECATION=1.
EOF
    return 0
}
