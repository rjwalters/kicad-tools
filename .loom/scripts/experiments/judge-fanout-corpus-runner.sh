#!/usr/bin/env bash
#
# judge-fanout-corpus-runner.sh — measurement-harness SCAFFOLD (issue #3748).
#
# Purpose: make the DEFERRED Judge fan-out measurement mechanical for an operator
# running from a TOP-LEVEL Claude Code session. It builds, for each PR in a
# small corpus, the exact `args` object the Workflow tool needs
# (`{ pr, diff }`), by shelling out to `gh pr diff`. It then emits an empty
# results-table row per PR into a Markdown file the operator fills in with real
# numbers after invoking the workflow.
#
# CRITICAL — what this scaffold DOES NOT do (and a builder CANNOT do):
#   It does NOT invoke `Workflow({ scriptPath, args })`. The Workflow tool is
#   only exposed to a top-level Claude Code session; a script/subagent context
#   has no access to it, and driving it from a subagent would add a second
#   nesting level (#3289). The hook point where the operator invokes the workflow
#   is a clearly-marked, UNFILLED TODO below (emit_workflow_invocation_stub).
#   Filling it in is the operator's deferred action — see
#   docs/research/judge-fanout-measurement-runbook.md.
#
# This script is READ-ONLY with respect to GitHub: it only runs `gh pr diff`
# (and, with --with-outcome auto, `gh pr view`) — it applies no labels, merges
# nothing, and creates no issues. Its only writes are local artifact files under
# the chosen output directory.
#
# Off-by-default posture: like judge-fanout-experiment.sh this is experiment
# tooling. It is never wired into the production sweep/judge path.
#
# Usage:
#   judge-fanout-corpus-runner.sh --out DIR PR[:outcome] [PR[:outcome] ...]
#
#   PR            a PR number whose diff to fetch (already merged or rejected).
#   :outcome      optional known ground-truth label for that PR: `merged` or
#                 `rejected`. If omitted, the cell is left blank for the operator.
#
# Options:
#   --out DIR         output directory for per-PR args + the results table
#                     (default: ./judge-fanout-corpus-out).
#   --diff-only       only fetch diffs + build args; skip results-table emission.
#   --dry-run         print what would be fetched/written; touch nothing.
#   -h | --help       show this help.
#
# Example (operator, top-level session):
#   ./judge-fanout-corpus-runner.sh --out /tmp/jf 3721:merged 3699:rejected 3688:merged
#   # -> /tmp/jf/pr-3721.args.json  (…{ "pr": 3721, "diff": "<unified diff>" })
#   #    /tmp/jf/results.md          (empty table, one row per PR, to be filled)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_JS="${SCRIPT_DIR}/judge-fanout-workflow.js"
TEMPLATE_MD="${SCRIPT_DIR}/judge-fanout-results-template.md"

OUT_DIR="./judge-fanout-corpus-out"
DIFF_ONLY=0
DRY_RUN=0
PR_SPECS=()

usage() { sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --out=*) OUT_DIR="${1#*=}"; shift ;;
    --diff-only) DIFF_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "judge-fanout-corpus-runner: unknown option '$1'" >&2; exit 2 ;;
    *) PR_SPECS+=("$1"); shift ;;
  esac
done

if [[ ${#PR_SPECS[@]} -eq 0 ]]; then
  echo "judge-fanout-corpus-runner: no PRs given. See --help." >&2
  exit 2
fi

if [[ ! -f "${WORKFLOW_JS}" ]]; then
  echo "judge-fanout-corpus-runner: ERROR — workflow not found at ${WORKFLOW_JS}" >&2
  exit 1
fi

# --- helpers ----------------------------------------------------------------

# JSON-escape a string on stdin (no jq dependency for the scaffold's core path;
# jq is used only when available for the diff, to guarantee valid JSON).
json_escape() {
  if command -v jq >/dev/null 2>&1; then
    jq -Rs .
  else
    # Minimal fallback: escape backslash, double-quote, and control chars.
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
  fi
}

# parse "PR[:outcome]" -> sets PR_NUM and PR_OUTCOME globals.
parse_spec() {
  local spec="$1"
  PR_NUM="${spec%%:*}"
  if [[ "$spec" == *:* ]]; then PR_OUTCOME="${spec#*:}"; else PR_OUTCOME=""; fi
  if ! [[ "$PR_NUM" =~ ^[0-9]+$ ]]; then
    echo "judge-fanout-corpus-runner: invalid PR spec '$spec' (need PR[:outcome])" >&2
    exit 2
  fi
  case "$PR_OUTCOME" in
    "" | merged | rejected) ;;
    *) echo "judge-fanout-corpus-runner: outcome must be merged|rejected, got '$PR_OUTCOME'" >&2; exit 2 ;;
  esac
}

# Build the per-PR args JSON file from `gh pr diff`. Returns the args path.
build_args_for_pr() {
  local pr="$1" out="$2"
  local args_path="${out}/pr-${pr}.args.json"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] would fetch: gh pr diff ${pr}  ->  ${args_path}" >&2
    return 0
  fi
  local diff_json
  # gh pr diff is read-only. Capture the unified diff, JSON-escape it, and wrap.
  diff_json="$(gh pr diff "${pr}" 2>/dev/null | json_escape)"
  if [[ -z "$diff_json" || "$diff_json" == '""' ]]; then
    echo "judge-fanout-corpus-runner: WARNING — empty diff for PR ${pr} (skipping args)" >&2
    return 1
  fi
  printf '{\n  "pr": %s,\n  "diff": %s\n}\n' "${pr}" "${diff_json}" > "${args_path}"
  echo "${args_path}"
}

# ============================================================================
# HOOK POINT (UNFILLED — operator's deferred action, requires the Workflow tool)
# ============================================================================
# A builder CANNOT fill this in: `Workflow({...})` is a top-level-session tool.
# From a top-level Claude Code session, the operator replaces this stub with a
# real invocation, e.g. (pseudocode — the tool is not a shell command):
#
#     result = Workflow({
#       scriptPath: "<abs path to judge-fanout-workflow.js>",
#       args:       <contents of pr-<N>.args.json>,
#     })
#     # then record result.verdict / result.findings / droppedUnverified +
#     # measured latency + token cost into the results table.
#
# See docs/research/judge-fanout-measurement-runbook.md for the full procedure.
emit_workflow_invocation_stub() {
  local pr="$1" args_path="$2"
  cat >&2 <<STUB
  [PR ${pr}] args ready: ${args_path}
    TODO(operator): from a TOP-LEVEL session, invoke:
      Workflow({ scriptPath: "${WORKFLOW_JS}", args: <${args_path}> })
    then record verdict/findings/latency/token-cost in the results table.
    (This scaffold intentionally does NOT invoke the Workflow tool — #3289.)
STUB
}

# --- main -------------------------------------------------------------------

if [[ "$DRY_RUN" -eq 0 ]]; then
  mkdir -p "${OUT_DIR}"
fi

echo "judge-fanout-corpus-runner (#3748 scaffold): ${#PR_SPECS[@]} PR(s), out=${OUT_DIR}" >&2
echo "  READ-ONLY on GitHub (gh pr diff/view only); no Workflow tool invocation here." >&2

RESULTS_MD="${OUT_DIR}/results.md"
ROWS=()

for spec in "${PR_SPECS[@]}"; do
  parse_spec "$spec"
  args_path="$(build_args_for_pr "${PR_NUM}" "${OUT_DIR}")" || true
  if [[ "$DRY_RUN" -eq 0 && -n "${args_path:-}" && -f "${args_path}" ]]; then
    emit_workflow_invocation_stub "${PR_NUM}" "${args_path}"
  fi
  # One empty results row per PR. Ground-truth outcome filled if provided; every
  # measured column is left blank ("") for the operator — NO fabricated numbers.
  ROWS+=("| ${PR_NUM} | ${PR_OUTCOME:-} |  |  |  |  |  |  |  |")
done

if [[ "$DIFF_ONLY" -eq 1 ]]; then
  echo "judge-fanout-corpus-runner: --diff-only set; skipping results table." >&2
  exit 0
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] would write results table: ${RESULTS_MD}" >&2
  printf '%s\n' "${ROWS[@]}" >&2
  exit 0
fi

# Emit the results table: header copied from the template if present, else an
# inline header. Rows are appended EMPTY — the operator fills real numbers in.
{
  echo "# Judge fan-out measurement results (issue #3748 harness output)"
  echo
  echo "Generated by judge-fanout-corpus-runner.sh. Every measured cell is EMPTY;"
  echo "fill with REAL numbers after invoking the workflow from a top-level session."
  echo "See docs/research/judge-fanout-measurement-runbook.md. Do NOT fabricate data."
  echo
  echo "| PR # | Known outcome | Dimension findings | Verified findings | Precision (1 - unverified-nit rate) | Recall (dimensions caught) | Fan-out latency (s) | Token cost | Single-pass Judge baseline |"
  echo "|------|---------------|--------------------|-------------------|-------------------------------------|----------------------------|---------------------|------------|----------------------------|"
  printf '%s\n' "${ROWS[@]}"
} > "${RESULTS_MD}"

echo "judge-fanout-corpus-runner: wrote ${#ROWS[@]} empty row(s) -> ${RESULTS_MD}" >&2
echo "  (template reference: ${TEMPLATE_MD})" >&2
exit 0
