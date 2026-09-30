#!/usr/bin/env bash
# loom-daemon-update.sh - Self-update the RAW loom-daemon process (Issue #3968)
#
# THIN STUB. The implementation is `loom-daemon daemon-update` (Rust,
# `loom-daemon/src/daemon_update/`) as of epic #7810 (#8088, the epic's third
# and last port). This entry point survives because
# `scripts/shell-allowlist.txt` records it as an INVOCATION CONTRACT: the
# `loom` dispatcher, the daemon's own auto-update tick and operators all invoke
# it BY THIS PATH. Flags, the stdout/stderr split and the exit codes are
# unchanged.
#
# This port is also the permanent fix the #7794 comment block promised. That
# block captured this file's own leading comment into `_LOOM_HELP_BANNER` at
# startup because an `awk "$0"` pass from inside `show_help()` raced a
# same-path truncate+rewrite (a resync, a shared runner checkout, or this
# script's own `git merge --ff-only`) and printed a torn banner — a race that
# failed CI twice (#7201, PR #7768). The banner is now
# `loom-daemon/src/daemon_update/help.txt`, compiled into the binary by
# `include_str!`, so there is no file to re-read and no window to narrow.
#
# Exit codes (contract — the dispatcher, the daemon's auto-update tick and the
# retained suites all branch on these):
#   0  up to date (no-op), or rebuild/fetch + provision + restart succeeded
#   1  usage error / not a source checkout / build or provision failure / the
#      ff-only sync could not apply / an artifact verification failure /
#      `--fetch` with no resolvable artifact
#   3  (--check only) update available
#   4  build verification FAILED — the built binary embeds the wrong commit
#   5  post-provision verification FAILED — the destination is not what this
#      run produced
#   6  supervised restart REFUSED by the running (old) binary
#   7  restart ACK'd but the supervisor never relaunched, and the self-heal
#      also failed
#   8  drain fail-safe preserved — not a failure
#
# The full behavioural reference — every flag, every knob, the artifact-fetch
# mode and the incident behind each one — is `loom-daemon daemon-update
# --help`, rendered from `loom-daemon/src/daemon_update/help.txt`, which is
# this script's own pre-port comment block verbatim.
#
# requires-daemon: daemon-update >= 0.19.353   #8088 — the port itself, declared in the same PR that adds the subcommand. Hard, not `optional`: this stub only execs, it never probes or degrades. The number is the HIGHEST this PR may legally name, not the true first-shipping version: `daemon-update` first ships in the post-merge VERSION bump, which a PR may never write (#7743) and which check-daemon-subcommand-versions.sh rejects as a floor above VERSION. So this floor errs PERMISSIVE by however many bumps land between this line and the merge — a host inside that window still gets clap's bare "unrecognized subcommand", exactly the status quo for an undeclared stub, while every host below it gets the actionable roll command (#8385). On a rebase, raise it to the new VERSION; never lower it.
# LOOM_SCRIPT_HELPER_MISSING_RC is left at its default 1 on purpose: 1 is
# already this script's documented code for a failure that shipped nothing (see
# the exit codes above), and none of this entry point's codes carry data, so an
# unresolvable or below-floor daemon cannot be mistaken for an answer.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Two messages tell an operator to re-run this command with another flag
# (`--prune-stale-entry-points`, `--relaunch`), and what they typed is this
# script's path — never ~/.local/bin/loom-daemon, which `current_exe()` would
# report and which, mid-self-replacement, may not even name a file that still
# exists. The shell knew it as "$0"; export it so the port keeps naming the
# entry point that actually exists in muscle memory.
export LOOM_UPDATE_ARGV0="$0"

# The shell's own `$SCRIPT_DIR`, which #5140's self-location fallback used to
# answer "which checkout do I rebuild?" when $PWD is not inside one at all.
# The binary cannot re-derive it: `current_exe()` points at
# ~/.local/bin/loom-daemon, where no checkout lives. Exporting it keeps that
# fallback resolving the SAME checkout the shell resolved.
export LOOM_UPDATE_CLI_DIR="$SCRIPT_DIR"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/../lib/script-helper.sh"

# Guarded so `source`ing this file is a no-op: a stub that exec'd on source
# would replace the sourcing shell and run the subcommand with ITS arguments.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    loom_exec_script_helper daemon-update "$@"
fi
