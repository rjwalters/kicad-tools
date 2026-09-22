#!/usr/bin/env bash
# skip-labels.sh — combined "not a work item" label list for a role prompt's
# unfiltered fallback query (Issue #8255).
#
# THIN STUB. The implementation is `loom-daemon skip-labels` (Rust,
# `loom-daemon/src/cli/skip_labels.rs`) — see that module for why this exists:
# `hard-exclusion-labels.sh` is a fixed fleet-wide list a repo cannot extend,
# while `autonomous.workFinder.extraSkipLabels` (#6685) is the per-repo knob
# the daemon's own work finder already reads. This subcommand is the missing
# shell-facing union of both, e.g. 2AMLogic/2am's `journal` status label
# (upstream 2am#582/#625).
#
# Usage:
#   skip-labels.sh [--lines|--json|--jq-not|--search] [--repo-root PATH]
#
#     --lines      (default) one label name per line
#     --json       a JSON array, e.g. ["external","journal"]
#     --jq-not     a jq boolean expression TRUE when an issue carries none of
#                  the labels, for `gh issue list --jq 'select(...)'`
#     --search     gh/forge search qualifiers excluding the labels, e.g.
#                  -label:"external" -label:"journal"
#     --repo-root  defaults to the current directory
#
# With no `autonomous.workFinder.extraSkipLabels` configured, output is
# byte-identical to `hard-exclusion-labels.sh`'s.
#
# THE VERSION FLOOR (#8385, follow-up to #8285)
#
# On 2026-09-19 this exact script met this repo's own installed daemon —
# 0.19.179, predating the subcommand — and the ONLY thing the operator saw was
#
#     error: unrecognized subcommand 'skip-labels'
#
# because the body below resolved a binary and exec'd it with no version guard
# whatsoever. `loom_daemon_version_preflight` (lib/locate-daemon-bin.sh, beside
# the resolver whose answer it is checking) is the shared fix: it reads the
# `# requires-daemon:` marker out of THIS file and refuses with the floor, what
# the resolved binary actually reports, and the `--fetch` roll command. The
# marker is also the string scripts/check-daemon-subcommand-versions.sh
# enforces, so the version in the refusal and the version the gate checks are
# one source, not two copies.
#
# WHY THE PREFLIGHT CALL, not `loom_exec_script_helper`: this file is a
# Shape-A `stub` in scripts/shell-allowlist.txt, a category machine-checked on
# the requirement that its LAST code line IS the `exec`. Delegating the exec to
# the helper would forfeit that category; calling the standalone preflight one
# line above the exec keeps both — and needs no second `source`, because the
# preflight lives in the resolver library this stub already sources.
#
# 0.19.186 is where `loom-daemon skip-labels` first shipped: it landed in
# 268777b7 (#8315, merged 2026-09-19) when VERSION read 0.19.185, and the
# post-merge bump that first carried it was 0.19.186 (91af5554).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || pwd)"
# $LOOM_DAEMON_SELF_BIN ("the binary that IMPLEMENTS this stub") first, then the
# ordinary chain — the same precedence lib/script-helper.sh and premise-check.sh
# apply (#8134), and the seam an operator pins to test against an old build.
BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -z "$BIN" ]]; then
    echo "[ERROR] loom-daemon not found (needed for 'skip-labels')." >&2
    echo "Build it: cargo build --release --manifest-path loom-daemon/Cargo.toml" >&2
    echo "Or set LOOM_DAEMON_SELF_BIN / LOOM_DAEMON_BIN to an existing binary." >&2
    exit 1
fi

# requires-daemon: skip-labels >= 0.19.186   #8315 — the subcommand itself; below this floor the preflight below refuses with the roll command instead of letting clap emit "unrecognized subcommand 'skip-labels'" (#8385)
loom_daemon_version_preflight skip-labels "$BIN"
exec "$BIN" skip-labels "$@"
