#!/usr/bin/env bash
# check-stale-blocked.sh - Surface open `loom:blocked` issues whose block no
# longer holds, or was never documented (issue #8927).
#
# THIN STUB. The implementation is `loom-daemon check-stale-blocked` (Rust,
# `loom-daemon/src/stale_blocked.rs` + `loom-daemon/src/cli/stale_blocked.rs`);
# new logic goes there, per `.loom/docs/shell-language-policy.md` — this is
# brand-new logic, so it went straight into the daemon rather than starting as
# a script to be ported later (the `generate-agent-skills.sh` precedent). This
# entry point exists because `/loom:sweep` names the three sibling pre-wave
# advisories by path and this is the fourth.
#
# It is a NON-BLOCKING, advisory check, mirroring check-host-sleep.sh (#3350),
# check-main-freshness.sh (#3770) and check-quarantine-stashes.sh (#5185) in
# contract: strictly read-only (it never edits a label), and it ALWAYS exits 0
# — including when no loom-daemon resolves, which is why
# LOOM_SCRIPT_HELPER_MISSING_RC is 0 here rather than the usual 1.
#
# WHAT IT ANSWERS: `loom:blocked` is applied once and never re-examined, and a
# blocked issue is skipped by /loom:sweep and by Champion's promotion lane — so
# a label that outlives its cause removes an issue from every queue
# indefinitely (three issues suppressed ~11 months in the incident that filed
# #8927). It reuses dep-recheck-fingerprint.sh's reference extraction rather
# than adding a second parser, and reports two categories to stderr: a STALE
# BLOCK (the cited blocker has closed/merged) and an UNDOCUMENTED BLOCK (no
# parseable blocker reference anywhere in the body or comments).
#
# Usage:
#   ./.loom/scripts/check-stale-blocked.sh           # print warning (or nothing) and exit 0
#   ./.loom/scripts/check-stale-blocked.sh --quiet    # suppress the stdout one-liner
#   ./.loom/scripts/check-stale-blocked.sh --json     # one JSON object on stdout
#   ./.loom/scripts/check-stale-blocked.sh --help     # show usage
#
# Exit codes:
#   0 - Always, for every check result and every failed read. The one exception
#       is a MALFORMED FLAG, which clap rejects as a usage error before any
#       check runs: a caller bug, not a check result. The three sibling scripts
#       silently ignore an unknown argument instead; that divergence is
#       deliberate, because a typo'd `--quiet` silently ignored is how an
#       advisory stops advising without anyone noticing.

set -uo pipefail  # NOTE: no -e — this script must never exit non-zero

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd)" || REPO_ROOT="$PWD"

# 0, not the default 1: the advisory contract is "always exit 0", so the
# version-floor refusal below must land on 0 too. Read by
# loom_daemon_version_preflight.
# shellcheck disable=SC2034  # read by loom_daemon_version_preflight, sourced below
LOOM_SCRIPT_HELPER_MISSING_RC=0

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

# $LOOM_DAEMON_SELF_BIN ("the binary that IMPLEMENTS this stub") first, then the
# ordinary chain — the same precedence premise-check.sh and skip-labels.sh apply (#8134).
BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -z "$BIN" ]]; then
    echo "[stale-blocked] loom-daemon not found; skipping (advisory, exit 0)." >&2
    echo "[stale-blocked]   Build it: cargo build --release --package loom-daemon" >&2
    exit 0
fi

# requires-daemon: check-stale-blocked >= 0.19.411   #8927 — the subcommand itself; below this floor a resolved binary refuses with clap's "unrecognized subcommand" instead of the version-floor message the preflight below gives (which exits 0 here, per LOOM_SCRIPT_HELPER_MISSING_RC above).
loom_daemon_version_preflight check-stale-blocked "$BIN"
exec "$BIN" check-stale-blocked "$@"
