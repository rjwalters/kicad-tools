#!/usr/bin/env bash
# check-host-sleep.sh - Warn when the host may sleep during long-running orchestration.
#
# This is a NON-BLOCKING, advisory check. It prints a platform-aware warning
# to stderr (and a brief one-liner to stdout) when the host is configured in a
# way that could let it sleep mid-session, killing in-flight subagents (#3350).
#
# It is invoked at the start of /loom:sweep. It MUST NOT
# block — even if detection fails, it returns 0 and orchestration proceeds.
#
# Usage:
#   ./.loom/scripts/check-host-sleep.sh         # print warning (or nothing) and exit 0
#   ./.loom/scripts/check-host-sleep.sh --quiet # suppress the stdout one-liner
#   ./.loom/scripts/check-host-sleep.sh --help  # show usage
#
# Exit codes:
#   0 - Always. This script is advisory; it never blocks Loom.
#
# Background: On 2026-05-28 an overnight /sweep lost two curator subagents at
# the 33-minute mark when macOS entered Maintenance Sleep. The user had a
# user-idle sleep assertion held (Amphetamine's PreventUserIdleSystemSleep),
# but Maintenance Sleep (governed by powerd / DarkWake / Power Nap) is
# independent of user-idle assertions and tore down the local TCP sockets to
# api.anthropic.com. See issue #3350.
#
# IMPORTANT macOS caveat: `caffeinate -dimsu` does NOT reliably defeat
# Maintenance Sleep on Apple Silicon. The only truly reliable defenses on
# macOS are:
#   - `sudo pmset -c sleep 0` (disable sleep on AC entirely), or
#   - flip the sleep-manager's "allow system sleep when display is off" toggle
#     to OFF (Amphetamine, Lungo, etc.).
# We surface those as the recommended remediation, not `caffeinate`.

set -uo pipefail  # NOTE: no -e — this script must never exit non-zero

# ---------- output helpers ----------

# Colors (only when stderr is a tty)
if [[ -t 2 ]]; then
    YELLOW='\033[1;33m'
    RED='\033[1;31m'
    GREEN='\033[1;32m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    YELLOW=''
    RED=''
    GREEN=''
    BOLD=''
    NC=''
fi

QUIET=0
for arg in "$@"; do
    case "$arg" in
        --quiet|-q)
            QUIET=1
            ;;
        --help|-h)
            sed -n '2,32p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            # Unknown args are ignored — this script must never fail.
            ;;
    esac
done

warn() {
    # Print a multi-line warning block to stderr. Always returns 0.
    printf '%b\n' "$*" >&2 || true
}

info_oneliner() {
    # Print a single status line to stdout (suppressed by --quiet).
    if [[ "$QUIET" -eq 0 ]]; then
        printf '%b\n' "$*" || true
    fi
}

# ---------- platform detection ----------

PLATFORM="$(uname -s 2>/dev/null || echo unknown)"

case "$PLATFORM" in
    Darwin)
        check_macos() {
            # Find the AC-power sleep timeout from `pmset -g`. Format is
            #   sleep                1 (sleep prevented by ...)
            # The first numeric column is minutes. 0 = sleep disabled.
            local pmset_out sleep_line sleep_minutes
            if ! command -v pmset >/dev/null 2>&1; then
                info_oneliner "${YELLOW}[sleep-check] pmset not found; cannot verify host sleep settings.${NC}"
                return 0
            fi

            pmset_out="$(pmset -g 2>/dev/null || true)"
            if [[ -z "$pmset_out" ]]; then
                info_oneliner "${YELLOW}[sleep-check] pmset returned no output; skipping check.${NC}"
                return 0
            fi

            # Extract the "sleep   <N>" line — leading whitespace, then "sleep".
            sleep_line="$(printf '%s\n' "$pmset_out" | awk '/^[[:space:]]*sleep[[:space:]]+[0-9]+/ {print; exit}')"
            if [[ -z "$sleep_line" ]]; then
                info_oneliner "${YELLOW}[sleep-check] could not parse pmset sleep timeout; skipping.${NC}"
                return 0
            fi

            sleep_minutes="$(printf '%s' "$sleep_line" | awk '{print $2}')"
            if ! [[ "$sleep_minutes" =~ ^[0-9]+$ ]]; then
                info_oneliner "${YELLOW}[sleep-check] unexpected pmset format; skipping.${NC}"
                return 0
            fi

            if [[ "$sleep_minutes" -eq 0 ]]; then
                info_oneliner "${GREEN}[sleep-check] macOS AC sleep is disabled (pmset sleep=0). Host should stay awake.${NC}"
                return 0
            fi

            # AC sleep is non-zero → the host CAN sleep on AC power.
            warn ""
            warn "${YELLOW}${BOLD}========================================================================${NC}"
            warn "${YELLOW}${BOLD}  WARNING: host may sleep during long-running orchestration (#3350)${NC}"
            warn "${YELLOW}${BOLD}========================================================================${NC}"
            warn "${YELLOW}macOS AC sleep timeout is ${sleep_minutes} minute(s) (pmset sleep=${sleep_minutes}).${NC}"
            warn "${YELLOW}If the host sleeps mid-run, in-flight subagent sockets to${NC}"
            warn "${YELLOW}api.anthropic.com will be torn down and that work will be lost.${NC}"
            warn ""
            warn "${BOLD}Important:${NC} ${RED}caffeinate -dimsu does NOT reliably defeat macOS"
            warn "Maintenance Sleep on Apple Silicon.${NC} A user-idle assertion (the kind"
            warn "Amphetamine and similar tools hold) was NOT enough to keep the host"
            warn "awake during the 2026-05-28 incident that motivated this check."
            warn ""
            warn "${BOLD}Reliable defenses on macOS:${NC}"
            warn "  - Disable AC sleep entirely (requires sudo, persistent):"
            warn "      ${BOLD}sudo pmset -c sleep 0${NC}"
            warn "  - Or, in your sleep manager (Amphetamine / Lungo / etc.), turn OFF"
            warn "    \"allow system sleep when display is off\" before this run."
            warn ""
            warn "Restore the default afterwards with:"
            warn "      ${BOLD}sudo pmset -c sleep 1${NC}  ${YELLOW}# or whatever value you prefer${NC}"
            warn "${YELLOW}========================================================================${NC}"
            warn ""

            info_oneliner "${YELLOW}[sleep-check] WARNING: macOS AC sleep is enabled (sleep=${sleep_minutes}m). See stderr for details.${NC}"
            return 0
        }
        check_macos
        ;;

    Linux)
        check_linux() {
            # Prefer systemd-inhibit if systemd is available.
            if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --version >/dev/null 2>&1; then
                info_oneliner "${YELLOW}[sleep-check] Linux without systemd detected; cannot auto-verify sleep settings.${NC}"
                info_oneliner "${YELLOW}[sleep-check] If this host can suspend, consider disabling suspend for this session.${NC}"
                return 0
            fi

            if ! command -v systemd-inhibit >/dev/null 2>&1; then
                info_oneliner "${YELLOW}[sleep-check] systemd-inhibit not found; cannot verify sleep inhibitors.${NC}"
                return 0
            fi

            local inhibit_list
            inhibit_list="$(systemd-inhibit --list 2>/dev/null || true)"

            # Look for any active sleep/idle inhibitor. The exact column layout
            # varies across systemd versions, so we just grep for the lock
            # types of interest in the listing text.
            if printf '%s' "$inhibit_list" | grep -Eq '(idle:sleep|sleep:idle|^[^:]*sleep)'; then
                info_oneliner "${GREEN}[sleep-check] Linux: an idle/sleep inhibitor is active. Host should stay awake.${NC}"
                return 0
            fi

            warn ""
            warn "${YELLOW}${BOLD}========================================================================${NC}"
            warn "${YELLOW}${BOLD}  WARNING: host may suspend during long-running orchestration (#3350)${NC}"
            warn "${YELLOW}${BOLD}========================================================================${NC}"
            warn "${YELLOW}No active idle/sleep inhibitor was found via systemd-inhibit --list.${NC}"
            warn "${YELLOW}If the host suspends mid-run, in-flight subagent sockets to${NC}"
            warn "${YELLOW}api.anthropic.com will be torn down and that work will be lost.${NC}"
            warn ""
            warn "${BOLD}Reliable defense on systemd Linux:${NC}"
            warn "  Wrap this whole session in:"
            warn "      ${BOLD}systemd-inhibit --what=idle:sleep --who=loom \\\\"
            warn "          --why='loom orchestration' -- <your command>${NC}"
            warn ""
            warn "  Example:"
            warn "      ${BOLD}systemd-inhibit --what=idle:sleep --who=loom --why=sweep -- \\\\"
            warn "          claude /sweep …${NC}"
            warn ""
            warn "  Or temporarily mask the sleep targets for this session:"
            warn "      ${BOLD}sudo systemctl mask sleep.target suspend.target \\\\"
            warn "          hibernate.target hybrid-sleep.target${NC}"
            warn "  (unmask afterwards with the same names)."
            warn "${YELLOW}========================================================================${NC}"
            warn ""

            info_oneliner "${YELLOW}[sleep-check] WARNING: no systemd-inhibit lock detected. See stderr for details.${NC}"
            return 0
        }
        check_linux
        ;;

    *)
        info_oneliner "${YELLOW}[sleep-check] Host platform '${PLATFORM}' unknown for sleep detection. Verify the host won't suspend during this run.${NC}"
        ;;
esac

# Always succeed — this script is advisory only.
exit 0
