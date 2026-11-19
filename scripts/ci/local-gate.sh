#!/usr/bin/env bash
# scripts/ci/local-gate.sh -- local CI-equivalent gate (Actions-outage backstop)
#
# Runs the same per-job gate scripts that .github/workflows/ci.yml calls, so
# the merge/release gate can be run locally when GitHub Actions is
# unavailable (issue #4671; the fallback *procedure* -- including the
# operator sign-off requirement -- is documented in RELEASING.md "Actions
# outage fallback"). This is the formalization of the ad-hoc gate used to
# ship v0.20.0 through the 2026-08-06 Actions outage.
#
# Since issue #9340 there is exactly ONE copy of each job's check commands,
# in scripts/ci/jobs/<job-id>.sh: the workflow's same-named job calls that
# script, and this gate runs the identical file (operator ruling written
# into 2AMLogic/2am#7561: the commands are written once, in a script in the
# repository, and the workflow calls that script -- never two copies, and no
# test comparing two copies). This file is only the runner: modes, SKIP and
# advisory handling, and the dispatch/report loop. The three genuinely
# CI-only jobs (`kicad_pin`, `changes`, `windows-smoke`) have no script and
# are not part of this gate.
#
# Usage:
#   scripts/ci/local-gate.sh [MODE] [JOB...]
#
# Modes (mutually exclusive; default is --cheap):
#   --cheap      Run the cheap gates only (site, lint, typecheck, test,
#                cpp-build-check, kicad-cli-smoke, routed-pcb-drc-check,
#                model-coverage-report). This is the default when no
#                mode/jobs are given.
#   --fast       Alias for --cheap.
#   --full       Run everything: cheap gates + the long board jobs
#                (multiple hours; each board job re-routes a board from
#                scratch).
#   --all        Alias for --full.
#   --release    Cheap gates + the release extras (board-03 routing
#                baseline test + changelog gap report) -- the exact set
#                used to gate the v0.20.0 release during the outage.
#   --list       Print the gate's job ids (one per line) and exit.
#                Release extras are NOT listed (they are not ci.yml jobs).
#   JOB...       Run only the named jobs. Any id from --list plus the
#                release-extra ids (release-board03-baseline,
#                release-changelog-gap) is accepted.
#
# Exit code: non-zero iff any non-advisory selected job FAILs. Advisory
# jobs (diffpair/matchgroup routing regressions -- advisory in CI too) and
# SKIPped jobs (e.g. kicad-cli absent) never flip the exit code.
#
# Local-vs-CI parity notes:
#   * CI runs the test / smoke / board jobs inside the pinned kicad/kicad
#     container. Locally you need kicad-cli (KiCad 10) on PATH for full
#     parity; jobs that strictly require it SKIP with a notice when it is
#     absent.
#   * Board jobs 03/04/06/07 stage freshly-routed artifacts over the
#     committed boards/*/output/ paths (the DRC gates key allowlist/mfr
#     overrides off the repo-relative path). CI runs in a throwaway
#     checkout; locally each job script backs the committed files up first
#     and restores them afterwards -- pass, fail, or Ctrl-C (the restore
#     runs from an EXIT/INT/TERM trap scoped to the staged window; see
#     scripts/ci/jobs/_lib.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
JOBS_DIR="$SCRIPT_DIR/jobs"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Job manifest -- the modes this gate runs. Each entry has a matching
# scripts/ci/jobs/<id>.sh which the ci.yml same-named job also calls, so
# there is no second command list to keep in sync. Order mirrors ci.yml
# declaration order for readable summaries.
# ---------------------------------------------------------------------------
CHEAP_JOBS=(
  site
  lint
  typecheck
  test
  cpp-build-check
  kicad-cli-smoke
  routed-pcb-drc-check
  model-coverage-report
)

LONG_JOBS=(
  test-extended
  diffpair-routing-regression
  matchgroup-routing-regression
  board-00-end-to-end
  board-01-end-to-end
  board-02-end-to-end
  board-03-end-to-end
  board-06-end-to-end
  board-07-end-to-end
  board-05-routing-regression
  board-04-end-to-end
)

# Advisory in CI: these jobs run on every PR but their failure does not
# block the merge button (see the branch-protection notes at ci.yml's
# diffpair-routing-regression / matchgroup-routing-regression jobs; the
# model-coverage-report job is `continue-on-error`, issue #7682).
# Mirrored here: they report FAIL but never flip the gate's exit code.
ADVISORY_JOBS=(
  model-coverage-report
  diffpair-routing-regression
  matchgroup-routing-regression
)

# Release extras -- NOT ci.yml jobs (so not in --list); part of --release.
# These are the two additional gates run for the v0.20.0 outage release
# (see RELEASING.md).
RELEASE_EXTRAS=(
  release-board03-baseline
  release-changelog-gap
)

# Sentinel return code meaning SKIP (EX_TEMPFAIL, unused by the tools we run).
# Job scripts exit with this (die_skip) when a tool they strictly require is
# absent locally.
SKIP_RC=75

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

usage() {
  # Print the header comment block (everything between the shebang and the
  # first non-comment line), stripped of the leading "# ".
  awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' \
    "${BASH_SOURCE[0]}"
}

print_list() {
  printf '%s\n' "${CHEAP_JOBS[@]}" "${LONG_JOBS[@]}"
}

is_known_job() {
  local candidate=$1 j
  for j in "${CHEAP_JOBS[@]}" "${LONG_JOBS[@]}" "${RELEASE_EXTRAS[@]}"; do
    [ "$j" = "$candidate" ] && return 0
  done
  return 1
}

is_advisory() {
  local candidate=$1 j
  for j in "${ADVISORY_JOBS[@]}"; do
    [ "$j" = "$candidate" ] && return 0
  done
  return 1
}

MODE=""
SELECTED=()
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --list) print_list; exit 0 ;;
    --cheap|--fast) MODE="cheap" ;;
    --full|--all) MODE="full" ;;
    --release) MODE="release" ;;
    --*)
      echo "Unknown option: $arg (see --help)" >&2
      exit 2
      ;;
    *)
      if ! is_known_job "$arg"; then
        echo "Unknown job id: $arg (see --list; release extras: ${RELEASE_EXTRAS[*]})" >&2
        exit 2
      fi
      SELECTED+=("$arg")
      ;;
  esac
done

if [ ${#SELECTED[@]} -gt 0 ] && [ -n "$MODE" ]; then
  echo "Give either a mode flag or explicit job ids, not both." >&2
  exit 2
fi

if [ ${#SELECTED[@]} -eq 0 ]; then
  case "${MODE:-cheap}" in
    cheap) SELECTED=("${CHEAP_JOBS[@]}") ;;
    full) SELECTED=("${CHEAP_JOBS[@]}" "${LONG_JOBS[@]}") ;;
    release) SELECTED=("${CHEAP_JOBS[@]}" "${RELEASE_EXTRAS[@]}") ;;
  esac
fi

# --- Environment preconditions -------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "FATAL: uv not found on PATH -- install uv first (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

DIRTY_TREE=0
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  DIRTY_TREE=1
fi

echo "local-gate: repo=$REPO_ROOT"
echo "local-gate: HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo '?') ($(git branch --show-current 2>/dev/null || echo 'detached'))"
if [ "$DIRTY_TREE" -eq 1 ]; then
  echo "local-gate: WARNING -- working tree is DIRTY; this run gates uncommitted state."
fi
if ! command -v kicad-cli >/dev/null 2>&1; then
  echo "local-gate: NOTE -- kicad-cli not on PATH; kicad-cli-smoke will SKIP and"
  echo "            kicad-cli-backed steps elsewhere may lose parity with the CI container."
fi
echo "local-gate: jobs: ${SELECTED[*]}"
echo

RESULTS_JOBS=()
RESULTS_STATUS=()
RESULTS_SECS=()
BLOCKING_FAILURES=0

for job in "${SELECTED[@]}"; do
  job_script="$JOBS_DIR/${job}.sh"
  if [ ! -f "$job_script" ]; then
    echo "FATAL: no job script at $job_script -- add scripts/ci/jobs/${job}.sh" >&2
    exit 1
  fi
  echo "=============================================================="
  echo "=== ${job}"
  echo "=============================================================="
  start=$SECONDS
  rc=0
  # LOAD-BEARING: do NOT "simplify" this back to `("bash $job_script") || rc=$?`.
  # Bash disables errexit inside any compound command that is an operand of
  # `||` / `&&` / `if`, and that suppression is inherited by the subshell and
  # every command it runs -- so only a job's LAST command would decide
  # PASS/FAIL (a failing `ruff format --check` followed by a passing
  # `ruff check` reported PASS). Running the subshell as its own statement,
  # with an explicit `set -e` inside it (the outer `set +e` is inherited),
  # keeps errexit live for the child script; the job scripts themselves also
  # run under `set -euo pipefail` of their own. The SKIP sentinel still
  # works: a script's `die_skip` exits it with SKIP_RC (75).
  # Regression-guarded by tests/test_local_gate_manifest.py.
  set +e
  (
    set -e
    bash "$job_script"
  )
  rc=$?
  set -e
  elapsed=$((SECONDS - start))
  if [ "$rc" -eq 0 ]; then
    status="PASS"
  elif [ "$rc" -eq "$SKIP_RC" ]; then
    status="SKIP"
  elif is_advisory "$job"; then
    status="FAIL (advisory)"
  else
    status="FAIL"
    BLOCKING_FAILURES=$((BLOCKING_FAILURES + 1))
  fi
  RESULTS_JOBS+=("$job")
  RESULTS_STATUS+=("$status")
  RESULTS_SECS+=("$elapsed")
  echo
  echo ">>> ${job}: ${status} (${elapsed}s)"
  echo
done

echo "=============================================================="
echo "local-gate summary"
echo "=============================================================="
printf '%-34s %-16s %s\n' "JOB" "RESULT" "SECONDS"
for i in "${!RESULTS_JOBS[@]}"; do
  printf '%-34s %-16s %s\n' "${RESULTS_JOBS[$i]}" "${RESULTS_STATUS[$i]}" "${RESULTS_SECS[$i]}"
done
echo
if [ "$DIRTY_TREE" -eq 1 ]; then
  echo "WARNING: working tree was DIRTY during this run (uncommitted state gated)."
fi
if [ "$BLOCKING_FAILURES" -gt 0 ]; then
  echo "RESULT: FAIL (${BLOCKING_FAILURES} blocking job(s) failed)"
  exit 1
fi
echo "RESULT: PASS (no blocking failures)"
