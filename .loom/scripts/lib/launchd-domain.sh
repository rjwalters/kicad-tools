#!/usr/bin/env bash
# launchd-domain.sh — shared launchd-domain resolver for the loom-daemon
# lifecycle scripts (issue #4130).
#
# Source this file (do not exec). Defines a single function:
#
#   resolve_launchd_domain -> echoes the launchd domain target
#     ("gui/<uid>", "user/<uid>", or an explicit LOOM_LAUNCHD_DOMAIN override)
#     that the loom-daemon LaunchAgent is loaded / inspected / booted out under.
#
# WHY THIS EXISTS (#4130). loom-daemon-start.sh historically hardcoded
# `gui/<uid>` — the per-GUI-login (Aqua) launchd domain owned by loginwindow.
# Over SSH with no active GUI login session that domain does not exist, so
# `launchctl bootstrap gui/<uid> …` fails with `error 125: Domain does not
# support specified action`, and the daemon cannot be (re)started remotely on a
# headless Mac. `user/<uid>` is the BACKGROUND per-user domain that sshd itself
# instantiates: it is present over SSH with no GUI at all, runs as the *user*
# (not root, unlike a system LaunchDaemon), and `gui/<uid>` already forwards to
# it (`endpoint destination = …domain.user.<uid>`). So we PREFER `gui/<uid>`
# when it resolves — byte-for-byte the pre-#4130 behavior on a host with a live
# GUI login — and fall back to `user/<uid>` only when it does not.
#
# REJECTED ALTERNATIVES (documented, deliberately NOT implemented):
#   * a system-domain LaunchDaemon — runs as root by default and has NO
#     login-keychain session, so it re-opens the #4005 headless-credential
#     problem in a new place instead of closing one; and
#   * `launchctl asuser <uid> launchctl bootstrap gui/<uid> …` — still needs a
#     target session to impersonate and needs sudo.
# `user/<uid>` is the smallest change and the closest privilege match to
# today's behavior. See daemon-reference.md "macOS session-bootstrap hazard
# (#3972)" for the trade-off table and the login-keychain (#4005) / TCC (#3980)
# consequences of a non-Aqua domain.
#
# Resolution precedence (first match wins):
#   1. $LOOM_LAUNCHD_DOMAIN — an explicit operator override, honored VERBATIM.
#      No probe, no further fallback: a domain pinned here that does not resolve
#      fails loudly at the launchctl call site (e.g. `launchctl bootstrap` in
#      loom-daemon-start.sh) rather than silently falling back to another
#      domain. The override is honored, not advisory.
#   2. gui/<uid> — when `launchctl print gui/<uid>` resolves (a live GUI login).
#   3. user/<uid> — the background per-user domain (headless / SSH-only).
#
# All four lifecycle scripts (start / stop / update / watchdog) source this ONE
# definition so they always agree on the domain — a per-script domain string is
# exactly how stop/update/watchdog would end up looking a service up in a
# domain the start put it somewhere else.
resolve_launchd_domain() {
    local uid
    uid="$(id -u)"
    if [[ -n "${LOOM_LAUNCHD_DOMAIN:-}" ]]; then
        printf '%s\n' "${LOOM_LAUNCHD_DOMAIN}"
        return 0
    fi
    if command -v launchctl >/dev/null 2>&1 && launchctl print "gui/${uid}" >/dev/null 2>&1; then
        printf '%s\n' "gui/${uid}"
        return 0
    fi
    printf '%s\n' "user/${uid}"
    return 0
}
