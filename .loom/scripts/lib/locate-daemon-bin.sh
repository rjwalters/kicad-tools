#!/usr/bin/env bash
# locate-daemon-bin.sh — Resolve the loom-daemon binary to invoke.
#
# Source this file (do not exec). Defines two functions:
#
#   loom_locate_daemon_bin <repo_root> -> echoes the absolute path to a
#   loom-daemon binary on stdout, or an empty string if none could be
#   resolved.
#
#   loom_daemon_bin_search_paths <repo_root> -> echoes the ordered list of
#   locations that WOULD be checked (one per line, in precedence order),
#   for use in "binary not found" error text. Does not touch the
#   filesystem itself -- it just renders the same candidate list
#   loom_locate_daemon_bin walks.
#
# Resolution precedence (first match wins):
#   1. $LOOM_DAEMON_BIN — must be executable.
#   2. Only when $LOOM_PREFER_REPO_BUILD=1: the build-output-relative
#      candidates under <repo_root> (see step 4 below), hoisted above the
#      installed binary so a developer who just `cargo build`ed in a
#      checkout runs what they built instead of a stale
#      $HOME/.local/bin/loom-daemon (#4997). Off by default — the plain
#      `loom-daemon-start.sh` / `.loom/bin/loom` production path must keep
#      preferring the machine-level install unconditionally.
#   3. `loom-daemon` on PATH.
#   4. The machine-level install location: $LOOM_DAEMON_BIN_DIR (default
#      $HOME/.local/bin) — this is where loom-daemon-update.sh's --provision
#      path installs, and the location `ssh host 'cmd'` (a non-interactive
#      shell that never sources the login profile, so $HOME/.local/bin is
#      NOT on PATH) needs an explicit check for (#4875).
#   5. Build-output-relative candidates under <repo_root>:
#        loom-daemon/target/release/loom-daemon
#        loom-daemon/target/debug/loom-daemon
#        target/release/loom-daemon
#        target/debug/loom-daemon
#
# Extracted (issue #4080) from the identical inline copies in
# loom-daemon-start.sh and loom-daemon-update.sh so probe-tokens.sh's
# daemon-binary resolution does not add a fourth copy of this logic. #4875
# added the machine-level install fallback (step 4) plus
# loom_daemon_bin_search_paths(), and migrated the remaining inline copies in
# loom-daemon-start.sh / loom-daemon-watchdog.sh / loom-daemon-update.sh /
# loom-status.sh / .loom/bin/loom onto this single shared definition so a
# future new candidate path never has to be ported by hand across six copies
# again. #4997 added the diagnostic stderr line (every resolution names the
# path + provenance + mtime it landed on, so "which binary ran" is always
# answerable from a sweep/session log) and the opt-in $LOOM_PREFER_REPO_BUILD
# precedence hoist (step 2).

# Best-effort mtime, formatted for a human-readable log line. GNU `stat -c`
# first (illegal option on BSD/macOS, so it fails cleanly there), then BSD
# `stat -f`. Echoes "unknown" if neither works (e.g. the path vanished
# between resolution and logging) rather than failing the caller.
_loom_daemon_bin_mtime_human() {
    local path="$1" epoch
    epoch="$(stat -c %Y "$path" 2>/dev/null || true)"
    if [[ ! "$epoch" =~ ^[0-9]+$ ]]; then
        epoch="$(stat -f %m "$path" 2>/dev/null || true)"
    fi
    if [[ ! "$epoch" =~ ^[0-9]+$ ]]; then
        echo "unknown"; return 0
    fi
    date -r "$epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
        || date -d "@$epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
        || echo "unknown"
}

loom_locate_daemon_bin() {
    local root="$1"
    local resolved="" via=""

    if [[ -n "${LOOM_DAEMON_BIN:-}" && -x "${LOOM_DAEMON_BIN}" ]]; then
        resolved="${LOOM_DAEMON_BIN}"
        via="\$LOOM_DAEMON_BIN"
    fi

    if [[ -z "$resolved" && "${LOOM_PREFER_REPO_BUILD:-}" == "1" ]]; then
        local repo_candidate
        for repo_candidate in \
            "$root/loom-daemon/target/release/loom-daemon" \
            "$root/loom-daemon/target/debug/loom-daemon" \
            "$root/target/release/loom-daemon" \
            "$root/target/debug/loom-daemon"; do
            if [[ -x "$repo_candidate" ]]; then
                resolved="$repo_candidate"
                via="repo-local build (\$LOOM_PREFER_REPO_BUILD=1)"
                break
            fi
        done
    fi

    if [[ -z "$resolved" ]] && command -v loom-daemon >/dev/null 2>&1; then
        resolved="$(command -v loom-daemon)"
        via="\$PATH"
    fi

    if [[ -z "$resolved" ]]; then
        local machine_bin="${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}/loom-daemon"
        if [[ -x "$machine_bin" ]]; then
            resolved="$machine_bin"
            via="machine-level install (\${LOOM_DAEMON_BIN_DIR:-\$HOME/.local/bin})"
        fi
    fi

    if [[ -z "$resolved" ]]; then
        local candidate
        for candidate in \
            "$root/loom-daemon/target/release/loom-daemon" \
            "$root/loom-daemon/target/debug/loom-daemon" \
            "$root/target/release/loom-daemon" \
            "$root/target/debug/loom-daemon"; do
            if [[ -x "$candidate" ]]; then
                resolved="$candidate"
                via="repo-local build"
                break
            fi
        done
    fi

    if [[ -n "$resolved" ]]; then
        echo "loom_locate_daemon_bin: resolved $resolved via $via (mtime: $(_loom_daemon_bin_mtime_human "$resolved"))" >&2
    fi

    echo "$resolved"
}

# loom_daemon_bin_search_paths <repo_root> -- render the candidate list for
# error messages. Mirrors loom_locate_daemon_bin's precedence exactly (kept
# side by side in this same file so the two can never drift apart).
loom_daemon_bin_search_paths() {
    local root="$1"
    if [[ -n "${LOOM_DAEMON_BIN:-}" ]]; then
        echo "\$LOOM_DAEMON_BIN=${LOOM_DAEMON_BIN}"
    fi
    if [[ "${LOOM_PREFER_REPO_BUILD:-}" == "1" ]]; then
        echo "$root/loom-daemon/target/release/loom-daemon (\$LOOM_PREFER_REPO_BUILD=1)"
        echo "$root/loom-daemon/target/debug/loom-daemon (\$LOOM_PREFER_REPO_BUILD=1)"
        echo "$root/target/release/loom-daemon (\$LOOM_PREFER_REPO_BUILD=1)"
        echo "$root/target/debug/loom-daemon (\$LOOM_PREFER_REPO_BUILD=1)"
    fi
    echo "loom-daemon on \$PATH"
    echo "${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}/loom-daemon"
    echo "$root/loom-daemon/target/release/loom-daemon"
    echo "$root/loom-daemon/target/debug/loom-daemon"
    echo "$root/target/release/loom-daemon"
    echo "$root/target/debug/loom-daemon"
}
