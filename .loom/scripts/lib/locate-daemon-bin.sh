#!/usr/bin/env bash
# locate-daemon-bin.sh — Resolve the loom-daemon binary to invoke.
#
# Source this file (do not exec). Defines four functions — and the split
# between the first pair and the second pair is the point (#8134):
#
#   $LOOM_DAEMON_BIN      the daemon a caller MANAGES or PROBES — the install
#                         whose version is compared, the socket endpoint a
#                         watchdog round-trips, a deliberately fake binary in a
#                         test. Resolved by loom_locate_daemon_bin below.
#   $LOOM_DAEMON_SELF_BIN the daemon that IMPLEMENTS the caller's own logic —
#                         what a Shape-A stub execs its subcommand on.
#                         Resolved by loom_daemon_self_bin_override /
#                         loom_resolve_self_daemon_bin further down.
#
# One variable cannot mean both: a suite that pins $LOOM_DAEMON_BIN to a probe
# mock would otherwise also redirect the stub into that mock (the #8134 hang).
# Which one a new caller wants is a question about the caller, not about the
# repo: "is this the binary I run, or the binary I inspect?"
#
#   loom_locate_daemon_bin <repo_root> -> echoes the absolute path to a
#   loom-daemon binary on stdout, or an empty string if none could be
#   resolved.
#
#   loom_daemon_bin_search_paths <repo_root> -> echoes the ordered list of
#   locations that WOULD be checked (one per line, in precedence order),
#   for use in "binary not found" error text. Does not write to the
#   filesystem -- it just renders the same candidate list
#   loom_locate_daemon_bin walks (which, since #6208, can include a
#   read-only `cargo metadata` call -- see _loom_daemon_repo_candidates()).
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
#   5. Build-output-relative candidates under <repo_root>, honoring a
#      redirected $CARGO_TARGET_DIR / build.target-dir (#6208):
#        $CARGO_TARGET_DIR/release/loom-daemon (if $CARGO_TARGET_DIR is set)
#        $CARGO_TARGET_DIR/debug/loom-daemon   (if $CARGO_TARGET_DIR is set)
#        loom-daemon/target/release/loom-daemon
#        loom-daemon/target/debug/loom-daemon
#        target/release/loom-daemon
#        target/debug/loom-daemon
#        <cargo metadata's target_directory>/release/loom-daemon (fallback)
#        <cargo metadata's target_directory>/debug/loom-daemon   (fallback)
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
# precedence hoist (step 2). #6208 taught the repo-local candidates (steps 2
# and 5) to also honor a redirected $CARGO_TARGET_DIR / ~/.cargo/config.toml's
# build.target-dir (resolved via `cargo metadata`, mirroring the build-time
# fix for loom-daemon-update.sh in #6160/#6209) instead of only ever probing
# the four historical hardcoded paths -- see _loom_daemon_repo_candidates().
#
# $LOOM_LOCATE_DAEMON_BIN_QUIET=1 suppresses the #4997 resolution-trace line
# above for a single call (default unset, i.e. the trace still prints — this
# is opt-in per-caller, not a global behavior change). Added for #6392: a
# caller that execs straight into the resolved binary (e.g.
# recover-orphaned-shepherds.sh) and inherits its stderr can otherwise leave
# this *success* trace as the only line an operator ever sees on a
# subsequent non-zero exit — read at a glance it looks like a failure
# reason, but it always reports a successful resolution. Set this only when
# the resolved binary's own stderr is the more useful signal; leave it unset
# anywhere "which binary ran" is itself the diagnostic (the common case).

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

# _loom_daemon_repo_candidates <repo_root> -- generates (one per line) the
# ordered list of repo-local build-output paths to probe for an EXISTING
# pre-built binary. This is the *discovery* counterpart to
# loom-daemon-update.sh's build-time artifact resolution (#6160/#6209): that
# fix parses `cargo build`'s own JSON output because it just ran a build;
# this helper has no build to parse output from, so it instead probes
# candidate paths -- including a $CARGO_TARGET_DIR redirect and a `cargo
# metadata`-resolved target_directory (which itself follows
# ~/.cargo/config.toml's build.target-dir), in addition to the historical
# hardcoded <repo>/loom-daemon/target and <repo>/target paths (#6208). Kept
# as a single generator so loom_locate_daemon_bin()'s two repo-local probe
# sites (steps 2 and 5 below) and loom_daemon_bin_search_paths() can never
# drift apart on candidate ordering.
_loom_daemon_repo_candidates() {
    local root="$1"

    # A $CARGO_TARGET_DIR redirect is cheap to check (no subprocess) and, if
    # set, is authoritative -- cargo itself would honor it, so probe it first.
    if [[ -n "${CARGO_TARGET_DIR:-}" ]]; then
        echo "$CARGO_TARGET_DIR/release/loom-daemon"
        echo "$CARGO_TARGET_DIR/debug/loom-daemon"
    fi

    echo "$root/loom-daemon/target/release/loom-daemon"
    echo "$root/loom-daemon/target/debug/loom-daemon"
    echo "$root/target/release/loom-daemon"
    echo "$root/target/debug/loom-daemon"

    # Only reached when $CARGO_TARGET_DIR is unset (already covered above)
    # and a loom-daemon crate manifest is present to key `cargo metadata`
    # off of. This is the only candidate that also catches a
    # ~/.cargo/config.toml build.target-dir redirect (an env var alone
    # can't see that -- only cargo itself resolves it), at the cost of one
    # subprocess call, so it is deliberately probed last.
    if [[ -z "${CARGO_TARGET_DIR:-}" && -f "$root/loom-daemon/Cargo.toml" ]] \
        && command -v cargo >/dev/null 2>&1; then
        local meta_target_dir
        meta_target_dir="$(cargo metadata --format-version 1 --no-deps \
            --manifest-path "$root/loom-daemon/Cargo.toml" 2>/dev/null \
            | grep -o '"target_directory":"[^"]*"' | head -n1 \
            | sed -E 's/^"target_directory":"//; s/"$//')"
        if [[ -n "$meta_target_dir" ]]; then
            echo "$meta_target_dir/release/loom-daemon"
            echo "$meta_target_dir/debug/loom-daemon"
        fi
    fi
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
        while IFS= read -r repo_candidate; do
            if [[ -n "$repo_candidate" && -x "$repo_candidate" ]]; then
                resolved="$repo_candidate"
                via="repo-local build (\$LOOM_PREFER_REPO_BUILD=1)"
                break
            fi
        done < <(_loom_daemon_repo_candidates "$root")
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
        while IFS= read -r candidate; do
            if [[ -n "$candidate" && -x "$candidate" ]]; then
                resolved="$candidate"
                via="repo-local build"
                break
            fi
        done < <(_loom_daemon_repo_candidates "$root")
    fi

    if [[ -n "$resolved" && "${LOOM_LOCATE_DAEMON_BIN_QUIET:-}" != "1" ]]; then
        echo "loom_locate_daemon_bin: resolved $resolved via $via (mtime: $(_loom_daemon_bin_mtime_human "$resolved"))" >&2
    fi

    echo "$resolved"
}

# loom_daemon_bin_search_paths <repo_root> -- render the candidate list for
# error messages. Mirrors loom_locate_daemon_bin's precedence exactly (both
# repo-local blocks below delegate to the shared _loom_daemon_repo_candidates
# generator so the two functions can never drift apart on ordering).
loom_daemon_bin_search_paths() {
    local root="$1" repo_candidate
    if [[ -n "${LOOM_DAEMON_BIN:-}" ]]; then
        echo "\$LOOM_DAEMON_BIN=${LOOM_DAEMON_BIN}"
    fi
    if [[ "${LOOM_PREFER_REPO_BUILD:-}" == "1" ]]; then
        while IFS= read -r repo_candidate; do
            [[ -n "$repo_candidate" ]] && echo "$repo_candidate (\$LOOM_PREFER_REPO_BUILD=1)"
        done < <(_loom_daemon_repo_candidates "$root")
    fi
    echo "loom-daemon on \$PATH"
    echo "${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}/loom-daemon"
    while IFS= read -r repo_candidate; do
        [[ -n "$repo_candidate" ]] && echo "$repo_candidate"
    done < <(_loom_daemon_repo_candidates "$root")
}

# loom_daemon_self_bin_override -- tier 1 of the IMPLEMENTATION resolution:
# $LOOM_DAEMON_SELF_BIN, when set AND executable.
#
# Echoes the path and returns 0 on a hit; echoes nothing and returns 1
# otherwise -- including "set but not executable", which falls through exactly
# as a non-executable $LOOM_DAEMON_BIN does in loom_locate_daemon_bin's step 1,
# so a typo'd pin degrades identically whichever variable carries it.
#
# Split out from loom_resolve_self_daemon_bin (#8134) because a caller that
# must NOT adopt the rest of that chain still needs this one tier.
#
# A Shape-A stub uses it as `loom_daemon_self_bin_override || loom_locate_daemon_bin
# "$root"`: tier 1, then the WHOLE normal chain. Deliberately not the rest of
# loom_resolve_self_daemon_bin's chain -- in production the installed daemon IS
# the implementation, $LOOM_DAEMON_BIN must keep pinning it (`loom update` and
# an operator debugging a stub both rely on that), and hoisting a checkout-local
# build above it would be the silent behaviour change #8134 rejected.
loom_daemon_self_bin_override() {
    [[ -n "${LOOM_DAEMON_SELF_BIN:-}" && -x "${LOOM_DAEMON_SELF_BIN}" ]] || return 1
    printf '%s\n' "$LOOM_DAEMON_SELF_BIN"
}

# loom_resolve_self_daemon_bin -- the loom-daemon that IMPLEMENTS a caller's
# ported logic, which is NOT the same binary loom_locate_daemon_bin names.
#
# The distinction is the whole point and it is easy to get wrong (#7977 caught
# it in a test fixture): loom_locate_daemon_bin / $LOOM_DAEMON_BIN name the
# INSTALLED daemon a script MANAGES -- the one whose version is compared, which
# may be an old release with no `retry-classify` / `release-resolve`
# subcommand at all, and which during a test is a deliberately fake binary.
# Exec'ing a ported subcommand on that is a category error.
#
# Resolution order, most explicit first:
#   1. $LOOM_DAEMON_SELF_BIN -- must be executable. The seam a test or an
#      operator uses to name the implementation directly.
#   2. A build in THIS checkout ($CARGO_TARGET_DIR honored, release then debug).
#      Script-relative first: this library lives at <checkout>/defaults/scripts/lib/
#      (or <consumer>/.loom/scripts/lib/), so the build that implements the
#      logic THIS COPY delegates to is the one in the checkout this copy came
#      from -- not whatever $REPO_ROOT happens to be (in a test fixture,
#      $REPO_ROOT is the fixture, which has no build at all). $REPO_ROOT is
#      still probed after it, for the caller that has one.
#   3. `loom-daemon` on PATH.
# Echoes "" when none resolves; the caller then answers in its own contract
# (a fail-safe verdict, not a crash) rather than failing silently.
#
# Lives here, beside loom_locate_daemon_bin, so the two resolutions sit next to
# each other and the distinction above is unmissable. Extracted from
# cli/loom-daemon-update.sh by #8037, when claude-wrapper.sh became the second
# caller that needs the IMPLEMENTATION rather than the managed install.
loom_resolve_self_daemon_bin() {
    loom_daemon_self_bin_override && return 0
    local self_root
    self_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." 2>/dev/null && pwd)" || self_root=""
    local base candidate
    for base in "${CARGO_TARGET_DIR:-}" "${self_root:+$self_root/target}" \
                "${REPO_ROOT:+$REPO_ROOT/target}" \
                "${REPO_ROOT:+$REPO_ROOT/loom-daemon/target}"; do
        [[ -n "$base" ]] || continue
        for candidate in "$base/release/loom-daemon" "$base/debug/loom-daemon"; do
            if [[ -x "$candidate" ]]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done
    candidate="$(command -v loom-daemon 2>/dev/null || true)"
    [[ -n "$candidate" && -x "$candidate" ]] && printf '%s\n' "$candidate"
    return 0
}
