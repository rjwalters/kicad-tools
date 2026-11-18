#!/usr/bin/env bash
# scripts/ci/jobs/_lib.sh -- shared prelude and helpers for the per-job gate
# scripts in this directory (issue #9340).
#
# Each job script is the SINGLE copy of a ci.yml job's check commands: the
# workflow's same-named job calls it, and scripts/ci/local-gate.sh runs the
# same file as the Actions-outage fallback (operator ruling written into
# 2AMLogic/2am#7561: the commands are written once, in a script in the
# repository, and the workflow calls that script).
#
# Sourced, never executed directly. It provides:
#   REPO_ROOT   absolute repo root; the caller is cd'd there
#   step        prints a "=== name ===" header so a CI log still shows which
#               step failed after the job collapsed into one workflow step
#   SKIP_RC     sentinel exit code meaning SKIP (EX_TEMPFAIL; local-gate maps
#               it to SKIP, CI never sees it because kicad-cli/npm are always
#               present in the CI images)
#   die_skip    print a notice and exit $SKIP_RC
#   check_drc_over_committed_path <fresh> <committed>
#               stage a fresh routed PCB over its committed repo path (the
#               DRC gates key allowlist/mfr overrides off that path), run
#               scripts/ci/check_routed_drc_internal.py, and restore the
#               committed file afterwards -- pass, fail or signal. CI's
#               checkout is a throwaway workspace so the restore is a no-op
#               in practice; locally it keeps the working tree clean.
#   stage_over_committed_dir <fresh-dir> <committed-dir>
#               same backup/restore contract for a whole output directory
#               (board 04's reviewed paid-drill validator requires the fresh
#               manifest/board.json to travel with the fresh PCB).

# shellcheck shell=bash

JOBS_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
REPO_ROOT="$(cd "$JOBS_DIR/../../.." && pwd)"
cd "$REPO_ROOT" || exit

step() { printf '=== %s ===\n' "$*"; }

SKIP_RC=75

die_skip() {
  echo "$*" >&2
  exit "$SKIP_RC"
}

# Idempotent restore of a staged-over committed path (file or directory).
# Safe to call twice: the backup is removed on the first successful restore.
_restore_committed_from_backup() {
  local bak=$1 committed=$2
  [ -e "$bak" ] || return 0
  cp -a "$bak" "$committed"
  rm -rf "$bak"
}

# Shared backup/restore staging window: copy the committed path aside, stage
# the fresh artifact over it, run "$@" inside a subshell whose EXIT/INT/TERM
# traps restore the committed path (so Ctrl-C keeps aborting the run instead
# of being reported as a pass), then belt-and-braces restore again in case
# the subshell died without running its traps (SIGKILL).
_with_staged_committed_path() {
  local committed=$1
  shift
  local bak
  bak="$(mktemp "${TMPDIR:-/tmp}/ci-jobs-stage-bak.XXXXXX")"
  cp -a "$committed" "$bak"
  local rc=0
  (
    trap '_restore_committed_from_backup "$bak" "$committed"' EXIT
    trap 'trap - INT EXIT; _restore_committed_from_backup "$bak" "$committed"; kill -INT "$BASHPID"' INT
    trap 'trap - TERM EXIT; _restore_committed_from_backup "$bak" "$committed"; kill -TERM "$BASHPID"' TERM
    "$@"
  ) || rc=$?
  _restore_committed_from_backup "$bak" "$committed"
  return "$rc"
}

check_drc_over_committed_path() {
  local fresh=$1 committed=$2
  _with_staged_committed_path "$committed" bash -c '
    set -euo pipefail
    cp "$1" "$2"
    shift 2
    "$@"
  ' _ "$fresh" "$committed" \
    uv run python scripts/ci/check_routed_drc_internal.py "$committed"
}

stage_over_committed_dir() {
  local fresh_dir=$1 committed_dir=$2
  shift 2
  _with_staged_committed_path "$committed_dir" bash -c '
    set -euo pipefail
    cp -a "$1"/. "$2"/
    shift 2
    "$@"
  ' _ "$fresh_dir" "$committed_dir" "$@"
}
