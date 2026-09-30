#!/usr/bin/env bash
# check-merge-config.sh — advisory: can merge-pr.sh actually merge in this repo?
# (issue #9287)
#
# merge-pr.sh depends on forge-side configuration that lives outside the repo
# tree: the repository's allow_* merge flags AND every active branch ruleset's
# allowed_merge_methods / required_linear_history. When those disagree, every
# merge fails (HTTP 405) several phases downstream of the cause. This check
# reports that at install (scripts/install-loom.sh, Step 5c) and on every
# resync (resync-installed.sh), before a Builder -> Judge cycle is spent.
#
# Findings (see `loom-daemon forge merge-config --help`):
#   EMPTY_EFFECTIVE_SET            repo methods ∩ every active ruleset's = {}
#   LINEAR_HISTORY_REJECTS_MERGE   required_linear_history + merge-only set
#   METHOD_NOT_PERMITTED           the method merge-pr.sh will use is not usable
#
# Contract: READ-ONLY (never writes a ruleset or repository setting) and ALWAYS
# exits 0 — an unreadable ruleset (403, no auth) is "could not determine",
# never a finding and never a failure. Silent when there is nothing to report.
#
# Shape-A stub (ADR-0018): the logic is the `loom-daemon forge merge-config`
# subcommand; this file only resolves the binary. A missing binary, or one that
# predates the subcommand, is a skipped check — noted on stderr, still exit 0.
# Findings and "could not determine" notes go to stdout.
#
# Usage:
#   check-merge-config.sh [--repo OWNER/NAME] [--branch B] [--method M] [--verbose]
#
# Env: LOOM_DAEMON_BIN overrides the loom-daemon binary (default: PATH lookup).
#
# requires-daemon: forge optional   `forge merge-config --help` is probed first; a missing or older binary prints a stderr skip note and exits 0 (#9287)

set -uo pipefail

BIN="${LOOM_DAEMON_BIN:-loom-daemon}"

if ! command -v "$BIN" >/dev/null 2>&1; then
    echo "merge-config: skipped — loom-daemon not found ('$BIN'); merge configuration not checked." >&2
    exit 0
fi

if ! "$BIN" forge merge-config --help >/dev/null 2>&1; then
    echo "merge-config: skipped — '$BIN' predates 'forge merge-config' (#9287); merge configuration not checked." >&2
    exit 0
fi

exec "$BIN" forge merge-config "$@"
