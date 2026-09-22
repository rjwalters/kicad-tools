#!/usr/bin/env bash
# loom-daemon-start.sh - Safe start wrapper for the RAW loom-daemon process
# (the autonomous work-finder + main-health-gate host — epic #3809, Phase D
# #3813).
#
# THIN STUB. The implementation is `loom-daemon daemon-start` (Rust,
# `loom-daemon/src/daemon_start/`) as of epic #7810 (#8087). This entry point
# survives because `scripts/shell-allowlist.txt` records it as an INVOCATION
# CONTRACT: the `loom` dispatcher, the daemon's own restart paths and operators
# all invoke it BY THIS PATH. Flags, stdout/stderr shape and exit codes are
# unchanged.
#
# Default is FLAGS-OFF: a bare `loom-daemon-start.sh` does NOT auto-dispatch
# sweeps. That is a deliberate safe default — enable autonomy explicitly with
# --work-finder / --health-gate, or hand control to .loom/config.json with
# --from-config.
#
# Exit codes (contract — the dispatcher and the retained suite both branch on
# these):
#   0  daemon started (or already running)
#   1  usage error / binary not found / daemon failed to start / a DETECTED
#      autonomy downgrade refused pending an explicit flag (#5409) / a real
#      start from a shell carrying agent-session context that would write the
#      DEFAULT supervisor identity (#6568)
#
# The full behavioural reference — every flag, every knob, and the incident
# behind each one — is `loom-daemon daemon-start --help`, rendered from
# `loom-daemon/src/daemon_start/help.txt`.
#
# requires-daemon: daemon-start >= 0.19.285   #8087 — the port itself, declared in the same PR that adds the subcommand. Hard, not `optional`: this stub only execs, it never probes or degrades. The number is the HIGHEST this PR may legally name, not the true first-shipping version: `daemon-start` first ships in the post-merge VERSION bump, which a PR may never write (#7743) and which check-daemon-subcommand-versions.sh rejects as a floor above VERSION. So this floor errs PERMISSIVE by however many bumps land between this line and the merge — a host inside that window still gets clap's bare "unrecognized subcommand", exactly the status quo for an undeclared stub, while every host below it gets the actionable roll command (#8385). On a rebase, raise it to the new VERSION; never lower it.
# LOOM_SCRIPT_HELPER_MISSING_RC is left at its default 1 on purpose: 1 is
# already this script's documented code for "binary not found" (see the exit
# codes above), so an unresolvable or below-floor daemon cannot be mistaken for
# data — none of this entry point's exit codes carry any.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The refusal messages tell an operator how to re-run the command, and what
# they typed is this script's path — never ~/.local/bin/loom-daemon, which
# `current_exe()` would report. The shell knew it as "$0"; export it so the
# port keeps naming the entry point that actually exists in muscle memory.
export LOOM_START_ARGV0="$0"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/../lib/script-helper.sh"

# Guarded so `source`ing this file is a no-op: a stub that exec'd on source
# would replace the sourcing shell and run the subcommand with ITS arguments.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    loom_exec_script_helper daemon-start "$@"
fi
