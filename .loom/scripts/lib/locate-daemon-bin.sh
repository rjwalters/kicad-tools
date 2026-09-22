#!/usr/bin/env bash
# locate-daemon-bin.sh — Resolve the loom-daemon binary to invoke.
#
# Source this file (do not exec). Defines the two resolution pairs below —
# and the split between the first pair and the second pair is the point
# (#8134) — plus the capability/version questions about whatever they resolved
# (loom_daemon_model_select_flag, #8058; loom_daemon_version_preflight /
# loom_daemon_exec_checked, #8385):
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

# loom_daemon_model_select_flag <daemon_bin> <model> -- echo `--model <model>`
# (two words) when per-model-class token selection is BOTH wanted and
# supported, or nothing at all otherwise. Issue #8058.
#
# Lives here, next to the binary resolver, because it answers a question about
# the RESOLVED BINARY: a daemon mid-roll that predates #8058 has no `--model`
# on `tokens select`, and clap rejects an unknown argument outright -- which
# would turn a routine binary/script version skew into a hard spawn failure on
# every dispatch. Same capability-probe idiom `spawn-claude.sh` already applies
# to `--auto-unpin` (#4228), and the same reason.
#
# Echoes nothing when:
#   * <model> is empty (session default -- selection stays account-wide), or
#   * the resolved binary's `tokens select --help` does not advertise `--model`.
#
# The caller splits the output on whitespace, so a model containing spaces is
# not supported -- no Claude model alias or pinned ID has ever contained one.
loom_daemon_model_select_flag() {
    local daemon_bin="$1" model="${2:-}" help_text
    [[ -n "$daemon_bin" && -n "$model" ]] || return 0
    # Captured into a variable rather than piped into `grep -q`: an early-exit
    # pipe consumer under `set -o pipefail` can SIGPIPE the producer and report
    # the whole pipeline as failed (see scripts/check-pipefail-early-exit.sh).
    help_text="$("$daemon_bin" tokens select --help 2>&1 || true)"
    [[ "$help_text" == *"--model"* ]] || return 0
    printf '%s %s' "--model" "$model"
}

# ---------------------------------------------------------------------------
# MARKER-DRIVEN DAEMON-VERSION PREFLIGHT (#8385, follow-up to #8285)
# ---------------------------------------------------------------------------
#
# A stub whose only statement is `exec "$bin" <sub>` has no version guard at
# all: against a binary that predates its subcommand, clap's own
#
#     error: unrecognized subcommand 'skip-labels'
#
# is the entire signal -- it names neither the version to roll to nor the
# command to roll with. That is exactly what happened to `skip-labels.sh` on
# 2026-09-19 against this repo's own installed 0.19.179. #8285 gave merge-pr.sh
# an actionable refusal for the same class; the functions below are the shared
# version, for `loom_exec_script_helper` and for any stub that execs a
# subcommand on a binary this file just resolved.
#
# WHY HERE and not in lib/script-helper.sh. This answers a question about the
# RESOLVED BINARY -- "is the thing I just found new enough to serve this call?"
# -- which is the same question `loom_daemon_model_select_flag` above already
# answers for `tokens select --model` (#8058), and it sits one line downstream
# of the resolution it depends on. It is also `bootstrap` work by definition:
# the binary being diagnosed is precisely the one too old to host the check as
# a subcommand, the same circularity `check-daemon-subcommand-versions.sh`
# argues for itself in scripts/shell-allowlist.txt. Putting it in the
# `contract` helper instead would have added portable shell that epic #7810 is
# retiring, to do a job that can never leave shell.
#
# OPT-IN BY DECLARATION, not by flag. The preflight fires only when the CALLING
# stub carries a `# requires-daemon: <this subcommand> >= <version>` marker --
# the same marker `scripts/check-daemon-subcommand-versions.sh` already
# enforces and `merge-pr.sh` already reads back at refusal time. A stub that
# declares nothing, declares `optional` (it probes and degrades), or declares a
# floor for a DIFFERENT subcommand is byte-identically unaffected, so this can
# be adopted one file at a time across the eleven-stub family. The marker is
# read out of the caller's own source, so the floor in the message, the floor
# the CI gate checks, and the floor a reviewer sees are one string.
#
# FAILS OPEN, deliberately -- the opposite of merge-pr.sh's guards, for a
# reason worth stating. Those guards fail CLOSED because an empty answer there
# is indistinguishable from a real one and would silently close an unfinished
# issue. This is not a safety gate: its only job is to turn an unactionable
# error into an actionable one. So when the floor cannot be read, `--version`
# cannot be parsed, or the subcommand name is not marker-shaped, it does
# NOTHING and the exec proceeds -- reproducing today's behaviour exactly, with
# the subcommand itself still refusing correctly when it is genuinely absent.
# Refusing on an unreadable version would invent a brand-new failure mode on a
# host whose binary is probably fine, which is a strictly worse trade.
#
# LOOM_SKIP_DAEMON_VERSION_PREFLIGHT=1 disables it wholesale, for a harness
# that deliberately pins an old binary.

# _loom_daemon_declared_floor <caller-file> <subcommand> -- echo the declared
# minimum version for <subcommand>, or nothing.
#
# The grammar is check-daemon-subcommand-versions.sh's, narrowed to the one
# subcommand asked about: `# requires-daemon: <sub> >= <major.minor.patch>`,
# optionally indented, optionally followed by a free-text note. An `optional`
# declaration deliberately does not match -- it is a statement that the stub
# copes without the subcommand, so there is no floor to enforce.
#
# `sed` with an early `q` rather than a bash read loop: one cheap subprocess
# that stops at the first marker, instead of iterating 3300 lines of
# worktree.sh in-shell on every WIP verb. <subcommand> is validated against
# ^[a-z][a-z0-9-]*$ by the caller BEFORE it reaches this interpolation, so no
# regex metacharacter can enter the script.
_loom_daemon_declared_floor() {
    local caller="$1" sub="$2"
    [[ -n "$caller" && -r "$caller" ]] || return 0
    sed -n "/^[[:space:]]*#[[:space:]]*requires-daemon:[[:space:]]*${sub}[[:space:]][[:space:]]*>=/{
                s/^[[:space:]]*#[[:space:]]*requires-daemon:[[:space:]]*${sub}[[:space:]][[:space:]]*>=[[:space:]]*\([0-9][0-9.]*\).*$/\1/p
                q
            }" "$caller" 2>/dev/null || true
}

# _loom_daemon_reported_version <bin> -- echo the semver `loom-daemon
# --version` reports, or nothing when it cannot be read. Never non-zero: every
# caller runs under `set -e`, and a best-effort diagnostic must not be able to
# abort the thing it is decorating (the same `|| true` discipline
# merge-pr.sh's hint documents).
_loom_daemon_reported_version() {
    local bin="$1" out=""
    [[ -n "$bin" && -x "$bin" ]] || return 0
    out="$("$bin" --version 2>/dev/null || true)"
    out="${out%%$'\n'*}"
    if [[ "$out" =~ ([0-9]+\.[0-9]+\.[0-9]+) ]]; then
        printf '%s' "${BASH_REMATCH[1]}"
    fi
    return 0
}

# _loom_daemon_version_lt <have> <want> -- true when have < want, compared
# NUMERICALLY per component. A string compare would read 0.19.9 as newer than
# 0.19.10 and 0.19.100 as older than 0.19.99, i.e. it would be wrong in both
# directions on exactly the two-digit-to-three-digit rollover this fleet lives
# on. Pure bash (no `sort -V` subprocess) and bash-3.2-clean, like every other
# helper here. A non-numeric component returns "not less" -- fail open.
_loom_daemon_version_lt() {
    local h="$1" w="$2" hp wp
    for _ in 1 2 3; do
        hp="${h%%.*}"
        wp="${w%%.*}"
        [[ "$hp" =~ ^[0-9]+$ && "$wp" =~ ^[0-9]+$ ]] || return 1
        (( 10#$hp < 10#$wp )) && return 0
        (( 10#$hp > 10#$wp )) && return 1
        case "$h" in *.*) h="${h#*.}" ;; *) h="0" ;; esac
        case "$w" in *.*) w="${w#*.}" ;; *) w="0" ;; esac
    done
    return 1
}

# _loom_daemon_version_preflight <caller-file> <subcommand> <bin>
#
# Returns 0 (proceed to the exec) in every case except one: the caller declares
# a floor, the binary reports a version, and that version is below the floor --
# then it prints the refusal and EXITS.
#
# The exit code is LOOM_SCRIPT_HELPER_MISSING_RC, the code each entry point
# already reserves for "could not run", never a fresh one. That is load-bearing
# and not stylistic: several of these subcommands use non-zero codes as DATA
# (`resolve-model --tier` exits 3 for "no mapping", `detect-dependency-cycle`
# exits 1 for "cycle found"), so a refusal wearing one of those codes would be
# read by the caller as an ANSWER -- the precise reason that variable exists.
_loom_daemon_version_preflight() {
    local caller="$1" sub="$2" bin="$3"
    local floor="" have="" caller_dir rc="${LOOM_SCRIPT_HELPER_MISSING_RC:-1}"

    [[ "${LOOM_SKIP_DAEMON_VERSION_PREFLIGHT:-0}" == "1" ]] && return 0
    [[ "$sub" =~ ^[a-z][a-z0-9-]*$ ]] || return 0

    floor="$(_loom_daemon_declared_floor "$caller" "$sub")"
    [[ -n "$floor" ]] || return 0

    have="$(_loom_daemon_reported_version "$bin")"
    [[ -n "$have" ]] || return 0

    _loom_daemon_version_lt "$have" "$floor" || return 0

    caller_dir="$(cd "$(dirname "$caller")" 2>/dev/null && pwd)" || caller_dir="."
    {
        printf '[ERROR] the resolved loom-daemon is too old to run `loom-daemon %s`.\n\n' "$sub"
        printf '  Required:  >= %s\n' "$floor"
        printf '  Resolved:  %s (reports %s)\n' "$bin" "$have"
        printf '  Declared by %s:\n      # requires-daemon: %s >= %s\n\n' "$caller" "$sub" "$floor"
        printf 'Refused BEFORE the call, so you get the floor and the fix rather than the\n'
        printf 'binary'"'"'s own bare argument-parser error, which names neither (#8285/#8385).\n\n'
        printf 'Roll THIS host, artifact-first:\n\n'
        printf '    %s/cli/loom-daemon-update.sh --fetch\n\n' "$caller_dir"
        printf '…which resolves the newest published release >= the installed version, verifies\n'
        printf 'its checksum (and signature when present), provisions it, and restarts the daemon\n'
        printf 'under its supervisor.\n\n'
        printf 'If no release artifact carries %s yet — releases are cut at fleet-rollable\n' "$floor"
        printf 'boundaries, not on every VERSION bump (.loom/docs/release-cadence.md) — build it:\n\n'
        printf '    cargo build --release -p loom-daemon\n'
        printf '    export LOOM_DAEMON_BIN=<repo>/target/release/loom-daemon\n\n'
        printf '…or pin LOOM_DAEMON_BIN (or LOOM_DAEMON_SELF_BIN) to an existing build that\n'
        printf 'already has `%s`. Confirm before re-running:\n\n' "$sub"
        printf '    %s --version && %s %s --help\n\n' "$bin" "$bin" "$sub"
        printf 'Exiting %s — this entry point'"'"'s "could not run" code, never an answer.\n' "$rc"
    } >&2
    exit "$rc"
}

# loom_daemon_version_preflight <subcommand> <resolved-bin>
#
# The PUBLIC, standalone form, for a stub that does its own resolution and its
# own `exec`. `skip-labels.sh` is the first: it is a Shape-A `stub` in
# scripts/shell-allowlist.txt, a category machine-checked on the requirement
# that its LAST code line IS the `exec` (scripts/check-shell-allowlist.sh), so
# it cannot hand the exec to a helper without forfeiting that category. One
# call one line above its own exec gets it the identical guard.
#
# ${BASH_SOURCE[1]} is the calling stub -- which is what lets the marker live
# in the stub that owns the dependency rather than in this library.
loom_daemon_version_preflight() {
    _loom_daemon_version_preflight "${BASH_SOURCE[1]:-$0}" "${1:-}" "${2:-}"
}

# loom_daemon_exec_checked <caller-file> <bin> <subcommand> [args...]
#
# Preflight, then `exec`. The form for a caller that resolves on behalf of
# somebody else -- `loom_exec_script_helper` in lib/script-helper.sh -- and so
# cannot let ${BASH_SOURCE[1]} name the stub whose marker matters. Never
# returns: either the preflight exits, or the exec replaces this shell, which
# is what keeps the subcommand's own exit code reaching the caller unmodified.
loom_daemon_exec_checked() {
    local caller="$1" bin="$2" sub="$3"
    shift 3
    _loom_daemon_version_preflight "$caller" "$sub" "$bin"
    exec "$bin" "$sub" "$@"
}
