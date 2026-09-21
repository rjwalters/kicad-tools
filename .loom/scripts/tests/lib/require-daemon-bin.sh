#!/usr/bin/env bash
# require-daemon-bin.sh — pin the `loom-daemon` binary a stub-driven suite tests
# against (epic #7810, PR 3 onwards).
#
# Source this file (do not exec). Defines:
#
#   loom_test_require_daemon_bin [--self-only] <scripts-dir> <subcommand> [<subcommand>...]
#       Resolves a loom-daemon, snapshots it to a private per-suite path,
#       exports LOOM_DAEMON_SELF_BIN (and, by default, LOOM_DAEMON_BIN) so
#       every stub this suite invokes execs THAT binary, and verifies it knows
#       each named subcommand. Never returns on failure — it exits 1 with an
#       actionable message.
#
# WHICH VARIABLE PINS THE STUB (#8134)
#
# LOOM_DAEMON_SELF_BIN is the pin that matters, and it is always exported: it
# means "the binary that IMPLEMENTS this stub", which `lib/script-helper.sh`
# now consults ahead of everything else. LOOM_DAEMON_BIN means something
# different — "the daemon this caller manages or probes" — and a suite whose
# subject invokes a daemon of its own uses it for exactly that.
#
# `--self-only` is for those suites: it pins the implementation and leaves
# LOOM_DAEMON_BIN alone, so the suite's own per-invocation
# `LOOM_DAEMON_BIN=<mock>` keeps its original meaning and its assertions need
# no edits. `loom-daemon-watchdog.sh` is the case this was built for — its
# retained suite pins a HANGING mock through LOOM_DAEMON_BIN to exercise the
# IPC probe, and a harness that exported LOOM_DAEMON_BIN over the top of it
# would both clobber that meaning and hand the stub the mock to exec.
#
# Without the flag BOTH are exported, which is what the five pure-computation
# suites already on this harness want: nothing in them reads LOOM_DAEMON_BIN
# for a second purpose, and keeping it exported preserves their behaviour
# exactly.
#
# WHY A SUITE NEEDS THIS
#
# A suite whose subject is now a thin stub over `loom-daemon <subcommand>` is
# only testing what it thinks it is if the stub execs the binary built from the
# working tree. Left to ambient resolution, `loom_locate_daemon_bin` prefers an
# installed `loom-daemon` on PATH (and, before that, `$LOOM_DAEMON_BIN`), so a
# stale machine-level install silently answers instead of the build under test.
# `LOOM_PREFER_REPO_BUILD=1` hoists the repo build above the install; in an
# installed consumer repo there is no repo build and this falls through to the
# installed binary exactly as before.
#
# WHY IT IS FATAL, NOT A SKIP
#
# Suites in this family carry assertions written against a SHELL implementation
# that has since been deleted. Running them against the port is the evidence
# that the port preserved its behaviour. A suite that quietly SKIPped itself
# when no binary resolved would remove that evidence from CI while still
# reporting green — the exact failure this epic keeps running into. If it
# cannot test the port, it fails and says why.
#
# The subcommand preflight exists for legibility: a binary predating the port
# makes every assertion exit 2 at once, which reads like a logic failure across
# the whole suite rather than one environment problem.
#
# WHY IT DOES NOT TEST WHATEVER HAPPENS TO BE LYING AROUND (#8176)
#
# `LOOM_PREFER_REPO_BUILD=1` answers "an installed binary must not win". It does
# NOT answer "which repo-local build", and on a host whose $CARGO_TARGET_DIR (or
# ~/.cargo/config.toml build.target-dir) is redirected to ONE directory shared by
# every checkout and every worktree, that second question has two wrong answers
# that both look plausible:
#
#   1. RELEASE-OVER-DEBUG. The shared candidate order probes release/ before
#      debug/, so a months-old `release/loom-daemon` outranks the `debug/`
#      binary a developer just built. The suite then runs assertions against a
#      binary that predates the change under test and reports a regression that
#      does not exist.
#   2. CROSS-WORKTREE CLOBBER. The path resolved here is shared, so a concurrent
#      `cargo build` in ANOTHER worktree can replace that file *after* this
#      function returns and *before* the suite's assertions run. Every later
#      assertion then execs a binary built from someone else's source.
#
# Both were observed for real while verifying #8119/PR #8174 (T15g/T15h "failed"
# against a foreign binary). Two structural answers, applied here and only here —
# the general-purpose `loom_locate_daemon_bin` that `loom-daemon-start.sh`,
# `loom-daemon-update.sh`, `loom-status.sh` and `.loom/bin/loom` share is
# untouched, because those callers must keep preferring the machine-level
# install unconditionally:
#
#   * FRESHEST WINS, not first-listed. Among the repo-local candidates that
#     exist, take the one with the newest mtime instead of the first in
#     precedence order. Equal mtimes keep the historical ordering, so nothing
#     changes on a host that has only ever built one profile.
#   * PRIVATE SNAPSHOT. Copy the resolved binary to a per-suite temp path and
#     pin THAT. A copy has its own inode, so a rebuild that replaces (or
#     deletes) the shared path mid-run cannot reach it. The copy is verified
#     stable — if the source changes while being copied, that IS case 2 firing
#     and the harness fails rather than testing a half-written binary.
#
# A third, weaker hazard survives both of those: a binary that is genuinely
# older than this checkout's Rust sources. Nothing here can fix that (only a
# rebuild can), so it is REPORTED — every resolution prints the binary's path,
# mtime and content fingerprint, and a binary older than `loom-daemon/src/**`
# gets a loud stale-binary warning naming the newer source file. It warns rather
# than failing by default on purpose: `git worktree add` stamps every source
# file with the checkout time, so in a fresh worktree mtime alone cannot tell
# "stale build" from "same source, freshly checked out", and a hard failure
# there would turn a diagnostic into a fleet-wide outage. Set
# LOOM_TEST_DAEMON_BIN_STRICT_FRESHNESS=1 to make it fatal where the caller
# knows a build just ran.
#
# Knobs (all default-off; the defaults are the behaviour described above):
#   LOOM_TEST_DAEMON_BIN_NO_SNAPSHOT=1        pin the resolved path itself, no
#                                             private copy. For a suite whose
#                                             subject depends on the binary's
#                                             own location (`current_exe()`),
#                                             and for debugging.
#   LOOM_TEST_DAEMON_BIN_STRICT_FRESHNESS=1   make the stale-binary warning fatal.
#   LOOM_TEST_DAEMON_BIN_QUIET=1              suppress the one-line resolution
#                                             trace (warnings still print).

# ---------------------------------------------------------------------------
# Small portability helpers. Every one of them is safe under `set -euo pipefail`
# (the five consumer suites all set it): each swallows its own failure and
# echoes an empty string rather than taking the suite down.
# ---------------------------------------------------------------------------

# _loom_test_daemon_bin_mtime <path> -- epoch mtime, or "" if unreadable.
# GNU `stat -c` first (an illegal option on BSD/macOS, so it fails cleanly
# there), then BSD `stat -f` -- the same two-step lib/locate-daemon-bin.sh uses.
_loom_test_daemon_bin_mtime() {
    local path="$1" epoch
    epoch="$(stat -c %Y "$path" 2>/dev/null || true)"
    [[ "$epoch" =~ ^[0-9]+$ ]] || epoch="$(stat -f %m "$path" 2>/dev/null || true)"
    [[ "$epoch" =~ ^[0-9]+$ ]] || epoch=""
    printf '%s\n' "$epoch"
}

# _loom_test_daemon_bin_size <path> -- byte count, or "".
_loom_test_daemon_bin_size() {
    local path="$1" size
    size="$(wc -c <"$path" 2>/dev/null | tr -dc '0-9' || true)"
    printf '%s\n' "$size"
}

# _loom_test_daemon_bin_identity <path> -- cheap "has this file changed?"
# token (size + mtime). Used to bracket the snapshot copy; a full hash on both
# sides would double the cost of the copy for no extra signal, since any
# rebuild that replaces the file changes its mtime.
_loom_test_daemon_bin_identity() {
    printf '%s:%s\n' "$(_loom_test_daemon_bin_size "$1")" "$(_loom_test_daemon_bin_mtime "$1")"
}

# _loom_test_daemon_bin_fingerprint <path> -- short content fingerprint, so a
# suite log answers "was this the SAME binary as the run before?" without
# anyone having to still have the file. Falls back to the byte count when no
# sha256 tool is present; never fails.
_loom_test_daemon_bin_fingerprint() {
    local path="$1" out=""
    out="$(shasum -a 256 "$path" 2>/dev/null | cut -c1-16 || true)"
    [[ -n "$out" ]] || out="$(sha256sum "$path" 2>/dev/null | cut -c1-16 || true)"
    [[ -n "$out" ]] || out="nosha:$(_loom_test_daemon_bin_size "$path")b"
    printf '%s\n' "$out"
}

# _loom_test_daemon_bin_mtime_human <path> -- mtime as a readable timestamp.
_loom_test_daemon_bin_mtime_human() {
    local epoch
    epoch="$(_loom_test_daemon_bin_mtime "$1")"
    [[ -n "$epoch" ]] || { printf '%s\n' "unknown"; return 0; }
    date -r "$epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
        || date -d "@$epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
        || printf '%s\n' "unknown"
}

# ---------------------------------------------------------------------------
# Resolution: freshest repo-local build wins (#8176 case 1)
# ---------------------------------------------------------------------------

# _loom_test_freshest_repo_build <repo_root> -- echo the NEWEST existing
# repo-local build-output candidate, or return 1 when none exists.
#
# Deliberately reuses lib/locate-daemon-bin.sh's own
# `_loom_daemon_repo_candidates` generator rather than re-listing paths: the
# set of places a build can land ($CARGO_TARGET_DIR, a `cargo metadata`
# target_directory, the historical hardcoded paths) must stay one definition,
# or this harness silently stops seeing a candidate the resolver gained. Only
# the CHOICE among them differs here — newest mtime rather than first listed —
# and a tie keeps the generator's order, so a host with exactly one build
# resolves precisely what it resolved before.
_loom_test_freshest_repo_build() {
    local root="$1" candidate best="" best_epoch=0 epoch
    while IFS= read -r candidate; do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        epoch="$(_loom_test_daemon_bin_mtime "$candidate")"
        [[ "$epoch" =~ ^[0-9]+$ ]] || epoch=0
        if [[ -z "$best" ]] || (( epoch > best_epoch )); then
            best="$candidate"
            best_epoch="$epoch"
        fi
    done < <(_loom_daemon_repo_candidates "$root" 2>/dev/null || true)
    [[ -n "$best" ]] || return 1
    printf '%s\n' "$best"
}

# _loom_test_daemon_bin_newer_source <repo_root> <bin> -- echo one
# loom-daemon source file newer than <bin>, or return 1. Absent
# `loom-daemon/src` (an installed consumer repo has none) means "no opinion".
_loom_test_daemon_bin_newer_source() {
    local root="$1" bin="$2" src="$1/loom-daemon/src" newer
    [[ -d "$src" ]] || return 1
    # `-print -quit` rather than `| head -n1`: a pipe to an early-exit consumer
    # under the caller's `set -o pipefail` is the SIGPIPE class
    # check-pipefail-early-exit.sh ratchets (#7790). `-quit` is in both GNU and
    # BSD find.
    newer="$(find "$src" -type f -newer "$bin" -print -quit 2>/dev/null || true)"
    [[ -n "$newer" ]] || return 1
    printf '%s\n' "$newer"
}

# ---------------------------------------------------------------------------
# Private snapshot: immune to a concurrent clobber (#8176 case 2)
# ---------------------------------------------------------------------------

_loom_test_daemon_bin_snapshot_root() {
    printf '%s\n' "${TMPDIR:-/tmp}/loom-test-daemon-bin"
}

# Reap snapshots whose owning shell has exited. Keyed on the PID baked into
# the directory name: a LIVE pid is always skipped, so this can never delete a
# running suite's binary out from under it (a recycled pid only defers a reap
# to the next run, which is harmless). This, not the EXIT trap below, is what
# actually bounds accumulation — a suite that installs its own EXIT trap after
# calling us drops ours.
_loom_test_reap_daemon_bin_snapshots() {
    local root="$1" dir pid
    [[ -d "$root" ]] || return 0
    for dir in "$root"/suite-*; do
        [[ -d "$dir" ]] || continue
        pid="${dir##*/suite-}"
        pid="${pid%%-*}"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            continue
        fi
        rm -rf "$dir" 2>/dev/null || true
    done
    return 0
}

_loom_test_cleanup_daemon_bin_snapshots() {
    local root
    root="$(_loom_test_daemon_bin_snapshot_root)"
    rm -rf "$root"/suite-"$$"-* 2>/dev/null || true
    return 0
}

# Install the cleanup trap ONLY when the suite has none of its own. Every
# consumer suite sets `trap cleanup EXIT` for its own workdir, and clobbering
# that would leak the thing it was cleaning up — a far worse trade than leaving
# a snapshot for the reaper above.
_loom_test_arm_daemon_bin_snapshot_cleanup() {
    local existing
    existing="$(trap -p EXIT 2>/dev/null || true)"
    [[ -z "$existing" ]] || return 0
    trap '_loom_test_cleanup_daemon_bin_snapshots' EXIT
    return 0
}

# _loom_test_snapshot_daemon_bin <bin> -- copy <bin> to a private per-suite
# path and echo that path.
#
# Exit codes are distinguished because the two failures deserve opposite
# handling: 2 means the environment could not host a snapshot (no writable
# temp dir, copy refused) and the caller degrades to the shared path with a
# warning; 3 means the source kept changing mid-copy, which IS the clobber
# hazard happening live, and the caller must fail rather than test a binary
# that was being rewritten as it was read.
_loom_test_snapshot_daemon_bin() {
    local bin="$1" root dir dest before_id after_id
    root="$(_loom_test_daemon_bin_snapshot_root)"
    mkdir -p "$root" 2>/dev/null || return 2
    _loom_test_reap_daemon_bin_snapshots "$root"
    dir="$(mktemp -d "$root/suite-$$-XXXXXX" 2>/dev/null || true)"
    [[ -n "$dir" && -d "$dir" ]] || return 2
    dest="$dir/loom-daemon"
    for _ in 1 2 3; do
        before_id="$(_loom_test_daemon_bin_identity "$bin")"
        if ! cp -p "$bin" "$dest" 2>/dev/null; then
            rm -rf "$dir" 2>/dev/null || true
            return 2
        fi
        after_id="$(_loom_test_daemon_bin_identity "$bin")"
        if [[ "$before_id" == "$after_id" ]]; then
            chmod +x "$dest" 2>/dev/null || true
            printf '%s\n' "$dest"
            return 0
        fi
    done
    rm -rf "$dir" 2>/dev/null || true
    return 3
}

# ---------------------------------------------------------------------------

loom_test_require_daemon_bin() {
    local self_only=0
    while [[ "${1:-}" == --* ]]; do
        case "$1" in
            --self-only) self_only=1; shift ;;
            *) echo "FATAL: loom_test_require_daemon_bin: unknown option '$1'" >&2; exit 1 ;;
        esac
    done

    local scripts_dir="$1"
    shift

    # shellcheck source=../../lib/locate-daemon-bin.sh
    source "$scripts_dir/lib/locate-daemon-bin.sh"

    LOOM_LOCATE_DAEMON_BIN_QUIET=1
    LOOM_PREFER_REPO_BUILD=1
    export LOOM_LOCATE_DAEMON_BIN_QUIET LOOM_PREFER_REPO_BUILD

    local repo_root bin="" via=""
    repo_root="$(cd "$scripts_dir/../.." && pwd)"

    # Precedence, deliberately mirroring loom_locate_daemon_bin's own first two
    # tiers so an operator's pin still wins, then diverging only in WHICH
    # repo-local build is chosen (see _loom_test_freshest_repo_build).
    if bin="$(loom_daemon_self_bin_override)"; then
        via="\$LOOM_DAEMON_SELF_BIN"
    elif [[ "$self_only" -eq 0 && -n "${LOOM_DAEMON_BIN:-}" && -x "${LOOM_DAEMON_BIN}" ]]; then
        # In --self-only mode $LOOM_DAEMON_BIN is the suite's PROBE binary, so
        # it must not answer "which binary implements the stub" — that is the
        # #8134 collision one level up, and the guard is the `self_only` test
        # in this condition.
        bin="${LOOM_DAEMON_BIN}"
        via="\$LOOM_DAEMON_BIN"
    elif bin="$(_loom_test_freshest_repo_build "$repo_root")"; then
        via="freshest repo-local build"
    else
        # No repo-local build at all — an installed consumer repo. Hand back to
        # the shared resolver for $PATH / the machine-level install, exactly as
        # before. $LOOM_DAEMON_BIN is unset for this one resolution (a command
        # substitution is already a subshell, so the caller's value is
        # untouched): an executable one already answered above, and in
        # --self-only mode it must never answer.
        bin="$(unset LOOM_DAEMON_BIN; loom_locate_daemon_bin "$repo_root")"
        via="ambient resolution"
    fi

    if [[ -z "$bin" ]]; then
        echo "FATAL: no loom-daemon binary found, so this suite cannot test the port." >&2
        echo "  Build it:  cargo build --package loom-daemon" >&2
        echo "  Or set:    LOOM_DAEMON_SELF_BIN=/path/to/loom-daemon" >&2
        exit 1
    fi

    local resolved_mtime fingerprint newer_src
    resolved_mtime="$(_loom_test_daemon_bin_mtime_human "$bin")"
    fingerprint="$(_loom_test_daemon_bin_fingerprint "$bin")"

    if newer_src="$(_loom_test_daemon_bin_newer_source "$repo_root" "$bin")"; then
        {
            echo "WARNING: the loom-daemon this suite resolved is OLDER than this checkout's Rust sources."
            echo "  binary:       $bin (mtime: $resolved_mtime, sha256: $fingerprint)"
            echo "  newer source: $newer_src"
            echo "  A failure below may be a STALE-BINARY artifact rather than a regression (#8176)."
            echo "  Rebuild before trusting it:  cargo build --package loom-daemon"
        } >&2
        if [[ "${LOOM_TEST_DAEMON_BIN_STRICT_FRESHNESS:-}" == "1" ]]; then
            echo "FATAL: LOOM_TEST_DAEMON_BIN_STRICT_FRESHNESS=1 — refusing to test a binary older than its source." >&2
            exit 1
        fi
    fi

    local pinned="$bin" snapshot_note="snapshot disabled (LOOM_TEST_DAEMON_BIN_NO_SNAPSHOT=1)"
    if [[ "${LOOM_TEST_DAEMON_BIN_NO_SNAPSHOT:-}" != "1" ]]; then
        local snapshot="" rc=0
        _loom_test_arm_daemon_bin_snapshot_cleanup
        snapshot="$(_loom_test_snapshot_daemon_bin "$bin")" || rc=$?
        case "$rc" in
            0)
                pinned="$snapshot"
                snapshot_note="snapshot: $pinned"
                ;;
            3)
                echo "FATAL: $bin changed on disk while this suite was copying it." >&2
                echo "Another build is writing that path right now — most likely a concurrent" >&2
                echo "worktree building into the same \$CARGO_TARGET_DIR. Anything this suite" >&2
                echo "reported would be about a half-written binary, not about your change (#8176)." >&2
                echo "  Re-run once that build finishes, or set LOOM_TEST_DAEMON_BIN_NO_SNAPSHOT=1" >&2
                echo "  to test the shared path directly." >&2
                exit 1
                ;;
            *)
                snapshot_note="snapshot UNAVAILABLE (no writable temp dir) — testing the shared path"
                echo "WARNING: could not snapshot $bin to a private path; a concurrent rebuild of that" >&2
                echo "  path can still swap the binary under this suite (#8176)." >&2
                ;;
        esac
    fi

    # Always pin the IMPLEMENTATION (#8134) — this is what every stub this
    # suite invokes now resolves first, whatever LOOM_DAEMON_BIN happens to
    # mean in this suite.
    export LOOM_DAEMON_SELF_BIN="$pinned"
    if [[ "$self_only" -eq 0 ]]; then
        export LOOM_DAEMON_BIN="$pinned"
    fi

    if [[ "${LOOM_TEST_DAEMON_BIN_QUIET:-}" != "1" ]]; then
        echo "loom_test_require_daemon_bin: resolved $bin via $via (mtime: $resolved_mtime, sha256: $fingerprint); $snapshot_note" >&2
    fi

    local sub
    for sub in "$@"; do
        if ! "$pinned" "$sub" --help >/dev/null 2>&1; then
            echo "FATAL: $bin does not know the '$sub' subcommand," >&2
            echo "so it predates the port this suite exists to verify." >&2
            echo "Rebuild it: cargo build --package loom-daemon" >&2
            exit 1
        fi
    done
}
