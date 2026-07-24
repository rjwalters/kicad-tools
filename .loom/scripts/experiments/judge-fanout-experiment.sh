#!/usr/bin/env bash
#
# judge-fanout-experiment.sh — off-by-default gate/runner for the #3739 design sketch.
#
# This is EXPERIMENT TOOLING ONLY. It does not touch the production judge path
# (defaults/roles/judge.md, defaults/.claude/commands/loom/{judge,sweep}.md), it
# applies no labels, merges nothing, and creates no GitHub issues.
#
# Flag contract (mirrors the LOOM_MODEL_EXPERIMENT / sweep.modelExperiment
# precedent — off by default, loud banner when on):
#
#   (unset) / 0 / false / no / off / ""   -> DISABLED: prints one line, exits 0 (no-op).
#   1 / true / yes / on                   -> ENABLED:  loud banner, syntax-checks the
#                                            sketch, prints the top-level-session
#                                            invocation guidance. Still dispatches
#                                            NO live judge run (see #3739 deferral).
#
# Why it never runs a live judge here: the runnable prototype must be driven from
# a TOP-LEVEL Claude Code session (so its agent()/parallel() calls are the first
# nesting level, #3289-safe). A subagent/script context cannot honestly exercise
# it. See docs/research/dynamic-workflows-evaluation.md → "What is deferred".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_JS="${SCRIPT_DIR}/judge-fanout-workflow.js"
CORPUS_RUNNER="${SCRIPT_DIR}/judge-fanout-corpus-runner.sh"

FLAG="${LOOM_JUDGE_FANOUT_EXPERIMENT:-}"

is_enabled() {
  case "$(printf '%s' "${FLAG}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

if ! is_enabled; then
  echo "judge-fanout-experiment: disabled (LOOM_JUDGE_FANOUT_EXPERIMENT unset) — no-op." >&2
  echo "  Set LOOM_JUDGE_FANOUT_EXPERIMENT=1 to enable the experiment tooling." >&2
  exit 0
fi

echo "============================================================================" >&2
echo "  LOOM JUDGE FAN-OUT EXPERIMENT (#3739) — DESIGN SKETCH, NOT PRODUCTION" >&2
echo "  This tooling touches NO production judge code, applies NO labels," >&2
echo "  merges NOTHING, and creates NO issues. Read-only, one level deep (#3289)." >&2
echo "============================================================================" >&2

if [[ ! -f "${WORKFLOW_JS}" ]]; then
  echo "judge-fanout-experiment: ERROR — sketch not found at ${WORKFLOW_JS}" >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "judge-fanout-experiment: node not found — skipping syntax check." >&2
else
  # The workflow body legitimately uses top-level await/return, which the Claude
  # Code workflow VM parses with allowReturnOutsideFunction/allowAwaitOutsideFunction.
  # To validate it faithfully WITHOUT executing it, wrap the body in an async
  # function and compile (never run) it via vm.Script.
  if node -e '
    const fs = require("fs");
    const vm = require("vm");
    const body = fs.readFileSync(process.argv[1], "utf8");
    // Compile-only: wrapping makes top-level await/return valid; we never run it.
    new vm.Script("(async () => {\n" + body + "\n})", { filename: process.argv[1] });
    console.error("judge-fanout-experiment: sketch syntax OK (compiled, not executed).");
  ' "${WORKFLOW_JS}"; then
    :
  else
    echo "judge-fanout-experiment: ERROR — sketch failed to compile." >&2
    exit 1
  fi
fi

# Syntax-check the measurement-harness scaffold too (#3748). It is a normal bash
# script (unlike the workflow VM body), so a plain `bash -n` parse suffices.
if [[ -f "${CORPUS_RUNNER}" ]]; then
  if bash -n "${CORPUS_RUNNER}"; then
    echo "judge-fanout-experiment: corpus-runner scaffold syntax OK (parsed, not executed)." >&2
  else
    echo "judge-fanout-experiment: ERROR — corpus-runner scaffold failed to parse." >&2
    exit 1
  fi
fi

cat >&2 <<'GUIDE'

To run the (deferred) live measurement — from a TOP-LEVEL Claude Code session only:
  1. Build the per-PR args + an empty results table with the harness scaffold:
       ./judge-fanout-corpus-runner.sh --out /tmp/jf 3721:merged 3699:rejected ...
     It shells out to `gh pr diff` (read-only) and writes pr-<N>.args.json plus
     results.md. It does NOT invoke the Workflow tool (that would need a second
     nesting level, #3289).
  2. Copy judge-fanout-workflow.js into a discovered workflows/ directory
     (user or project settings), OR invoke it directly with the Workflow tool:
       Workflow({ scriptPath: "<abs path to judge-fanout-workflow.js>",
                  args: <contents of pr-<N>.args.json> })
     Do this from a top-level session (NOT a subagent), so the workflow's
     agent()/parallel() calls are the first nesting level (#3289-safe). Never
     drive it from a `fable` session — the No-Fable-Judge invariant (#3702)
     applies to the fan-out reviewers.
  3. Record verdict / findings / droppedUnverified + measured latency + token
     cost into results.md (template: judge-fanout-results-template.md), and
     compare against today's single-pass Judge. Record RAW results — do not
     fabricate numbers.

  Full procedure: docs/research/judge-fanout-measurement-runbook.md

This script intentionally stops here: it does NOT dispatch a live judge run.
GUIDE

exit 0
