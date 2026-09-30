#!/usr/bin/env bash
# loom-daemon-watchdog.sh - Host-side autonomy-loss detector for the RAW
# loom-daemon process (issue #4011).
#
# THIN STUB. The implementation is `loom-daemon daemon-watchdog` (Rust,
# `loom-daemon/src/watchdog/`) as of epic #7810 (#8086). This entry point
# survives because a launchd/systemd job invokes it BY PATH on a StartInterval
# cadence, and because `loom-daemon-start.sh` writes that job's ProgramArguments
# pointing here. Flags, stdout/stderr shape and exit codes are unchanged.
#
# THE PROBLEM IT SOLVES
#   On 2026-07-26 the loom-daemon launchd job took a SIGTERM two seconds after
#   starting and was left `bootout`-ed (unloaded) from launchd. Autonomous
#   dispatch silently stopped. NOTHING surfaced it - no log line, no forge
#   signal, no notification. It was discovered hours later only because someone
#   happened to run `loom-daemon status` by hand. A pull nobody performs for
#   hours is not a detector.
#
#   This is the payload of a SECOND job (`<daemon-label>-watchdog`), separate
#   from the daemon job, that compares operator INTENT (the durable
#   `autonomy-desired` marker) against REALITY (is a daemon for the expected
#   label loaded and alive, is its heartbeat fresh, does its socket answer a
#   bounded IPC round-trip) and REPORTS loudly when they disagree.
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh            Check once, report on divergence
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh --verbose  Also log the healthy/idle no-op cases
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh --help
#
# Exit codes (contract - a supervisor and the retained suite both branch on these):
#   0  no divergence
#   1  DIVERGENCE - intent says a daemon should be running and reality
#      disagrees: not loaded, loaded but down, running with a stale heartbeat,
#      running but its bounded IPC round-trip failed (#4398), DEGRADED across
#      the recent windowed history (#5944), running while the marker is ABSENT
#      (#4331, crash protection disarmed), or answering while its supervisor
#      job is down (#5118, UNSUPERVISED).
#   2  usage error
#   3  (#5118) LIVENESS UNDETERMINED - deliberately NOT exit 1: no out-of-band
#      signal found a live daemon AND the in-band socket probe could not run
#      (no resolvable loom-daemon binary, the probe is disabled, or the CLI
#      never returned), so this tick has NO EVIDENCE either way. Reported as
#      UNKNOWN, never as "the daemon is down".
#
# The full behavioural reference - every knob, every state, and the incident
# behind each one - is `loom-daemon daemon-watchdog --help`, rendered from
# `loom-daemon/src/watchdog/help.txt`.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 3, not the default 1. Exit 1 from this script means DIVERGENCE, which a
# supervisor escalates on and which files a forge issue. An unresolvable binary
# is not evidence that the daemon is down - it is the absence of evidence, and
# the contract above already names "no resolvable loom-daemon binary" as exit 3
# / UNKNOWN. Letting it exit 1 would turn a broken install into a fake outage
# alarm, unattended, on a cadence.
# shellcheck disable=SC2034  # read by loom_exec_script_helper, sourced below
LOOM_SCRIPT_HELPER_MISSING_RC=3

# The implementation resolves sibling scripts — loom-daemon-start.sh for
# bounded recovery, create-issue.sh for escalation — RELATIVE TO THIS FILE, not
# to the binary. The shell knew its own directory as $_LOOM_WATCHDOG_CLI_DIR;
# the binary cannot derive it, because `current_exe()` points at
# ~/.local/bin/loom-daemon on every normal install, where no sibling scripts
# live. Export it so the port keeps the shell's meaning.
export LOOM_WATCHDOG_CLI_DIR="$SCRIPT_DIR"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/../lib/script-helper.sh"

# Guarded so `source`ing this file is a no-op: a stub that exec'd on source
# would replace the sourcing shell and run the subcommand with ITS arguments.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    loom_exec_script_helper daemon-watchdog "$@"
fi
