#!/usr/bin/env bash
# generate-agents-md.sh — generate defaults/.loom/AGENTS.md from the
# `agents-md:include` marker ranges in defaults/.loom/CLAUDE.md.
#
# Why (#4479, epic #4167 pillar 3): AGENTS.md is the cross-runtime instruction
# anchor (an Agentic AI Foundation / Linux Foundation standard read natively by
# OpenAI Codex CLI and others — see https://agents.md). Loom must not maintain a
# second hand-authored instruction file that drifts from CLAUDE.md, so this
# generator EXTRACTS the runtime-neutral repo-orientation content (label
# workflow, worktree rules, merge-pr.sh mandate, REST-vs-GraphQL guidance) that
# already lives in defaults/.loom/CLAUDE.md between `<!-- agents-md:include:start
# -->` / `<!-- agents-md:include:end -->` markers, and wraps it with AGENTS.md-
# specific framing (dual-runtime status + "Claude Code only today" callouts).
# There is exactly one place a human edits the prose: the CLAUDE.md source.
#
# The generated defaults/.loom/AGENTS.md is a CHECKED-IN artifact (same posture
# as defaults/.loom/CLAUDE.md itself: hand-edited inputs, machine-produced and
# committed output). scripts/check-agents-md-sync.sh fails CI if the checked-in
# file is stale relative to its source.
#
# Attribution: the install-time AGENTS.md scaffolding this feeds was seeded by
# the gpeyton/loom fork's PR #8 (AGENTS.md codegen). The fork shipped a
# hand-authored static file; this repo generates the content instead, per
# #4479's single-source non-goal.
#
# Usage:
#   generate-agents-md.sh              Write defaults/.loom/AGENTS.md (canonical)
#   generate-agents-md.sh --stdout     Print the generated content to stdout
#   generate-agents-md.sh <path>       Write to <path> instead of the canonical file
#
# Exit codes: 0 = generated; 1 = source missing / empty extraction;
#             2 = malformed or missing include markers in the source.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/../.loom/CLAUDE.md"
DEFAULT_OUT="$SCRIPT_DIR/../.loom/AGENTS.md"

START_MARKER="<!-- agents-md:include:start -->"
END_MARKER="<!-- agents-md:include:end -->"

MODE="file"
OUT="$DEFAULT_OUT"
case "${1:-}" in
  --stdout) MODE="stdout" ;;
  "") ;;
  *) OUT="$1" ;;
esac

if [[ ! -f "$SRC" ]]; then
  echo "generate-agents-md: source not found: $SRC" >&2
  exit 1
fi

# Extract the marked ranges. awk validates marker balance and fails loudly
# (exit 2) on malformed markers rather than silently emitting a wrong file:
#   - a start marker while already inside a range (nested / duplicate start)
#   - an end marker with no open start (end-before-start)
#   - an unclosed start marker at EOF
#   - no markers at all
# Multiple ranges are concatenated in document order, separated by one blank
# line so the ranges read as distinct sections.
extract() {
  awk -v s="$START_MARKER" -v e="$END_MARKER" '
    $0 == s {
      if (inside) {
        print "generate-agents-md: MALFORMED — nested start marker at line " NR > "/dev/stderr"
        exit 2
      }
      inside = 1
      starts++
      if (starts > 1) print ""
      next
    }
    $0 == e {
      if (!inside) {
        print "generate-agents-md: MALFORMED — end marker before start at line " NR > "/dev/stderr"
        exit 2
      }
      inside = 0
      ends++
      next
    }
    inside { print }
    END {
      if (inside) {
        print "generate-agents-md: MALFORMED — unclosed start marker (reached EOF)" > "/dev/stderr"
        exit 2
      }
      if (starts == 0) {
        print "generate-agents-md: MALFORMED — no agents-md:include markers found in source" > "/dev/stderr"
        exit 2
      }
    }
  ' "$SRC"
}

extracted="$(extract "$SRC")"

if [[ -z "${extracted//[[:space:]]/}" ]]; then
  echo "generate-agents-md: extraction produced empty content — check the markers in $SRC" >&2
  exit 1
fi

# Assemble the file: static framing header, the extracted body, static footer.
# The heredocs are single-quoted so the `{{INSTALL_DATE}}` placeholder and the
# backticks survive verbatim (substituted at install time by loom-daemon's
# scaffolding, exactly like the CLAUDE.md template). `{{LOOM_VERSION}}` is
# deliberately NOT emitted here any more (#8147) — see the header text below.
emit() {
  cat <<'HEADER'
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by defaults/scripts/generate-agents-md.sh from the
     `agents-md:include` marker ranges in defaults/.loom/CLAUDE.md. To change
     this file, edit those ranges in the source and re-run the generator;
     CI (scripts/check-agents-md-sync.sh) fails if this file is stale.
     Attribution: seeded by the gpeyton/loom fork's PR #8 (AGENTS.md codegen),
     ported to single-source generation for issue #4479 (epic #4167). -->

# Loom Orchestration - Repository Guide (AGENTS.md)

This repository uses **Loom** for AI-powered development orchestration.

**Installation Date**: {{INSTALL_DATE}}

> **Dual-runtime status**: this file is the runtime-neutral instruction anchor
> read by AGENTS.md-aware runtimes (OpenAI Codex CLI and others — see
> https://agents.md). Claude Code additionally reads the richer `.loom/CLAUDE.md`
> sibling as its primary format; the content below is a strict subset extracted
> from that same source, so the two files cannot diverge. Anything marked
> **Claude Code only today** (see the end of this file) is not yet portable to
> other runtimes — perform the equivalent `gh` / `git` / `./.loom/scripts/*`
> steps directly instead.

HEADER

  printf '%s\n' "$extracted"

  cat <<'FOOTER'

## Claude Code only today

The coordination mechanics above (labels, worktrees, the sweep lifecycle,
`merge-pr.sh`) are runtime-neutral — any worker that can run `gh` and `git`
participates. These dispatch surfaces are Claude-Code-specific for now:

- **Slash commands** (`/loom:sweep`, `/loom:builder`, `/loom:judge`, …) are
  defined under `.claude/commands/loom/`. A non-Claude runtime cannot invoke
  them by name; it performs the equivalent steps directly (claim the issue,
  create the worktree, implement, open the PR).
- **MCP tools** (`mcp__loom__dispatch_sweep`, `mcp__loom__list_sweeps`, …) are
  reachable only through Claude Code's MCP integration.
- **`PreToolUse` guard hooks** and the **`.loom/tokens/` multi-account token
  pool** guard Claude tool calls and rotate Claude Code credentials — they do
  not apply to other runtimes.

For the full Claude-Code-oriented guide (daemon MCP surface, configuration,
token pool, troubleshooting) see `.loom/CLAUDE.md`.

---

**Generated by Loom Installation Process**
Last updated: {{INSTALL_DATE}}
FOOTER
}

if [[ "$MODE" == "stdout" ]]; then
  emit
else
  emit > "$OUT"
  echo "generate-agents-md: wrote $OUT" >&2
fi
