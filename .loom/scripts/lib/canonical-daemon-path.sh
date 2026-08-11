#!/usr/bin/env bash
# canonical-daemon-path.sh — the single canonical, non-interactive-safe PATH
# superset shared by every place loom needs a deterministic PATH for `gh`,
# `cargo`, and Homebrew-installed tools without relying on a login shell's
# profile (issue #4831).
#
# WHY THIS EXISTS
#
# Before #4831 there were THREE independently hand-maintained, disagreeing
# partial PATH definitions:
#
#   - `resolve_plist_path()` (loom-daemon-start.sh) — the fullest one, baked
#     into every rendered launchd plist / systemd unit's `Environment=PATH=`
#     (issue #4172): `~/.local/bin`, `~/.cargo/bin`, Homebrew, standard dirs.
#   - `fleet add-worker`'s provisioning steps (loom-daemon/src/fleet/add_worker.rs)
#     — ~12 duplicated `export PATH="$HOME/.local/bin:$PATH"` lines, missing
#     both `~/.cargo/bin` and Homebrew.
#   - `fleet add-worker`'s rendered systemd unit (add_worker.rs) — a THIRD,
#     narrower `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`
#     that agreed with neither of the above.
#
# A subtly wrong subset in any one of them reproduces exactly the failure
# class that motivated this file: `gh`/`cargo`/homebrew-installed tools
# missing on a non-interactive SSH/launchd/systemd PATH (#4695, #4831).
#
# This file is the ONE place the full canonical superset is spelled out for
# shell. Every shell call site should source this file and call
# `canonical_daemon_path` rather than hand-roll its own subset:
#
#   - `loom-daemon-start.sh`'s `resolve_plist_path()` (the launchd plist /
#     systemd unit `Environment=PATH=` renderer)
#   - `loom-daemon-update.sh`'s cargo-not-found fallback (#4695)
#
# Rust call sites (`loom-daemon/src/fleet/add_worker.rs`'s provisioning-step
# templates, `loom-daemon/src/fleet/drain.rs`'s local `gh` invocations,
# `loom-daemon/src/fleet/status.rs`'s remote `loom-daemon status` invocation)
# cannot `source` a bash file at compile time, so they keep an
# independently-declared but byte-for-byte-equal copy in
# `loom-daemon/src/fleet/path_bootstrap.rs::CANONICAL_PATH_DIRS`. A Rust unit
# test (`path_bootstrap::tests::canonical_path_matches_shell_lib`) parses
# THIS file's `canonical_daemon_path()` body and fails the build the moment
# the two drift — so this file remains the single source of truth even
# though it cannot be executed from Rust.
#
# Source this file (do not exec) and call `canonical_daemon_path` — it prints
# the colon-joined PATH to stdout with no trailing newline. Callers decide
# whether to use it verbatim (a static `Environment=PATH=` line) or prepend
# it onto an inherited `$PATH` (an interactive/login-adjacent shell that
# might already have something useful earlier in `$PATH`).
canonical_daemon_path() {
    printf '%s' "${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
}
