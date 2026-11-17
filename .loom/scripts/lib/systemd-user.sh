#!/usr/bin/env bash
# systemd-user.sh — shared systemd --user resolver for the loom-daemon lifecycle
# scripts (issue #4268, sub-issue B of #4260).
#
# Source this file (do not exec). It is the Linux counterpart to
# lib/launchd-domain.sh: where launchd-domain.sh resolves the per-user launchd
# DOMAIN a macOS LaunchAgent loads under, this file answers the three questions
# the Linux systemd --user service path needs:
#
#   is_linux_systemd            -> return 0 when the systemd --user path should be
#                                  used (Linux + `systemctl` on PATH + a reachable
#                                  per-user manager), non-zero to fall back to the
#                                  legacy nohup path.
#   resolve_systemd_unit        -> echoes the unit name (default loom-daemon.service,
#                                  override via LOOM_SYSTEMD_UNIT).
#   resolve_systemd_unit_dir    -> echoes the user-unit directory (~/.config/systemd/user).
#   resolve_systemd_unit_path   -> echoes the full unit-file path (<dir>/<unit>).
#   systemd_user_manager_reachable -> return 0 when `systemctl --user` can talk to
#                                  the per-user manager (see the headless caveat).
#
# WHY THIS EXISTS (#4260/#4268). The pre-#4268 non-Darwin path in
# loom-daemon-start.sh was a plain `nohup "$DAEMON_BIN" &` — no reboot survival,
# no supervised restart, no bootout-on-stop. A `systemd --user` service mirrors
# the Darwin launchd contract (#3972/#4054/#4130/#4229) on Linux:
#
#   launchd (Darwin)                        systemd --user (Linux)
#   RunAtLoad=true + `launchctl enable`  ->  [Install] WantedBy=default.target
#                                            + `systemctl --user enable`
#   KeepAlive:{SuccessfulExit:true}      ->  Restart=on-success  (relaunch ONLY on
#     (relaunch only on clean exit 0)         a clean exit 0 — exact match; an
#                                             operator stop is never a clean exit,
#                                             so it is not auto-restarted)
#   `launchctl bootout` on operator stop ->  `systemctl --user disable --now <unit>`
#   plist EnvironmentVariables           ->  Environment= lines in the rendered unit
#   WorkingDirectory                     ->  WorkingDirectory=
#   --no-launchd / LOOM_DAEMON_LAUNCHD=0 ->  --no-systemd / LOOM_DAEMON_SYSTEMD=0
#     escape hatch to the nohup path          escape hatch to the nohup path
#
# HEADLESS / SSH CAVEAT (loginctl enable-linger). A `systemd --user` manager only
# runs while the user has a session, and the service only survives a reboot / a
# logout when the user has LINGERING enabled. Reboot survival and SSH-only hosts
# therefore require `loginctl enable-linger "$USER"` once; without it the unit is
# still installed + supervised for the life of the login session, but does not
# come back after a reboot. systemd_user_manager_reachable() below is the
# per-user-manager reachability probe (is XDG_RUNTIME_DIR present and does the
# manager answer): when it is false — a bare SSH login with no lingering and no
# active graphical/systemd user session — the start script warns and falls back
# to the nohup path rather than failing with a cryptic bus/bootstrap error.

# is_linux_systemd — return 0 when the systemd --user service path should drive
# the daemon lifecycle. Requires Linux, `systemctl` on PATH, and a reachable
# per-user manager.
#
# LOOM_SYSTEMD_FORCE=1 is a TEST-ONLY seam: it forces the platform + manager
# checks to pass (so the lifecycle tests can exercise the systemd branch on a
# Darwin CI runner behind a stub `systemctl` on PATH), while still requiring
# `systemctl` to be resolvable — so a test that forgets to install its stub does
# not silently take the branch.
is_linux_systemd() {
    if [[ "${LOOM_SYSTEMD_FORCE:-}" == "1" ]]; then
        command -v systemctl >/dev/null 2>&1
        return
    fi
    [[ "$(uname -s)" == "Linux" ]] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemd_user_manager_reachable
}

# systemd_user_manager_reachable — return 0 when `systemctl --user` can reach the
# per-user manager. A bare SSH login with no lingering and no active user session
# has no user manager to bootstrap into: XDG_RUNTIME_DIR is unset and
# `systemctl --user is-system-running` reports "offline" (or fails to connect to
# the bus). Both cases return non-zero here so the caller can warn + fall back to
# nohup instead of surfacing a cryptic "Failed to connect to bus" error.
systemd_user_manager_reachable() {
    [[ "${LOOM_SYSTEMD_FORCE:-}" == "1" ]] && return 0
    [[ -n "${XDG_RUNTIME_DIR:-}" ]] || return 1
    local state
    state="$(systemctl --user is-system-running 2>/dev/null)"
    # is-system-running exits non-zero when "degraded", but the manager is still
    # reachable then — key on the reported STATE, not the exit code. Only
    # "offline" (or an empty answer = no bus) means unreachable.
    case "$state" in
        offline|"") return 1 ;;
        *)          return 0 ;;
    esac
}

# resolve_systemd_unit — the unit name the loom-daemon service is installed under.
# Default loom-daemon.service; override with LOOM_SYSTEMD_UNIT (e.g. a test's
# scratch unit, or an operator managing a specifically-named daemon). A value
# without a `.service` suffix is left verbatim — systemctl treats a bare name as a
# .service unit, and the tests pin an explicit name, so no suffix normalization is
# imposed here.
resolve_systemd_unit() {
    echo "${LOOM_SYSTEMD_UNIT:-loom-daemon.service}"
}

# resolve_systemd_unit_dir — the per-user unit directory systemd reads from.
resolve_systemd_unit_dir() {
    echo "${HOME}/.config/systemd/user"
}

# resolve_systemd_unit_path — the full path of the rendered unit file.
resolve_systemd_unit_path() {
    echo "$(resolve_systemd_unit_dir)/$(resolve_systemd_unit)"
}
