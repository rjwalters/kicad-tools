#!/usr/bin/env bash
# hard-exclusion-labels.sh — the single shared source for Loom's HARD label
# exclusions (Issue #7528).
#
# A *hard exclusion* is a label that takes an issue out of the automated
# pipeline entirely: no role may curate it, build it, or promote it, and the
# daemon's autonomous work finder must not dispatch a sweep for it. `external`
# is the only one today — issues filed by non-collaborators (or auto-labeled by
# an intake workflow) that require a maintainer to remove the label before any
# agent touches them.
#
# WHY THIS FILE EXISTS
# --------------------
# Before #7528 the `external` exclusion was enforced ONLY inside the markdown
# role prompts (curator.md / builder.md `jq` filters). The Rust work finder
# knew nothing about it, so an issue carrying `loom:issue` + `external` was
# dispatched every tick, burned ~90s of a token account's session budget, was
# declined by the role prompt, and had its `loom:building` claim released back
# to `loom:issue` — 23 dispatches in one hour on rjwalters/kicad-tools#5197.
# Teaching the work finder about the list without consolidating it would have
# left the same literal duplicated in three places, guaranteeing future drift.
#
# So: this script is the shell-side accessor and the human-readable home of the
# list. The Rust side reads its own compile-time `HARD_EXCLUSION_LABELS` const
# (`loom-daemon/src/hard_exclusion.rs`) — a per-tick candidate filter must not
# shell out or depend on a resolved repo root — and a unit test in that module
# PARSES THIS FILE and fails the build if the two disagree. That test is the
# enforcement; this comment is not. Add or remove a label in ONE place (the
# array below, then let CI point you at the const) and never in a role prompt.
#
# Per-repo *additional* skip labels are a different, already-existing knob:
# `autonomous.workFinder.extraSkipLabels` (Issue #6685). This list is the
# fleet-wide floor that ships with Loom; that one is the local extension.
#
# Usage:
#   hard-exclusion-labels.sh [--lines|--json|--jq-not|--search]
#
#     --lines   (default) one label name per line
#     --json    a JSON array, e.g. ["external"]
#     --jq-not  a jq boolean expression that is TRUE when an issue carries
#               none of the labels, for use inside a `gh issue list --jq`
#               `select(...)`, e.g.
#                 ([.labels[].name] | contains(["external"]) | not)
#               Multiple labels are ANDed.
#     --search  gh/forge search qualifiers excluding the labels, e.g.
#                 -label:"external"
#
# Exit codes:
#   0 — printed
#   2 — usage error
set -uo pipefail

# ---------------------------------------------------------------------------
# THE LIST. Keep it in sync with `HARD_EXCLUSION_LABELS` in
# loom-daemon/src/hard_exclusion.rs (a unit test there enforces this).
# ---------------------------------------------------------------------------
HARD_EXCLUSION_LABELS=(
  external
)

_usage() {
  echo "Usage: hard-exclusion-labels.sh [--lines|--json|--jq-not|--search]" >&2
}

MODE="lines"
case "${1:---lines}" in
  --lines) MODE="lines" ;;
  --json) MODE="json" ;;
  --jq-not) MODE="jq-not" ;;
  --search) MODE="search" ;;
  -h|--help) _usage; exit 0 ;;
  *)
    echo "hard-exclusion-labels.sh: unknown option: $1" >&2
    _usage
    exit 2 ;;
esac

case "$MODE" in
  lines)
    printf '%s\n' "${HARD_EXCLUSION_LABELS[@]}"
    ;;
  json)
    _out=""
    for _l in "${HARD_EXCLUSION_LABELS[@]}"; do
      [[ -n "$_out" ]] && _out+=","
      _out+="\"${_l}\""
    done
    printf '[%s]\n' "$_out"
    ;;
  jq-not)
    _out=""
    for _l in "${HARD_EXCLUSION_LABELS[@]}"; do
      [[ -n "$_out" ]] && _out+=" and "
      _out+="([.labels[].name] | contains([\"${_l}\"]) | not)"
    done
    printf '%s\n' "$_out"
    ;;
  search)
    _out=""
    for _l in "${HARD_EXCLUSION_LABELS[@]}"; do
      [[ -n "$_out" ]] && _out+=" "
      _out+="-label:\"${_l}\""
    done
    printf '%s\n' "$_out"
    ;;
esac
