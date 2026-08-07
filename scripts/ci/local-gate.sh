#!/usr/bin/env bash
# scripts/ci/local-gate.sh -- local CI-equivalent gate (Actions-outage backstop)
#
# Mirrors the job set of .github/workflows/ci.yml so the merge/release gate
# can be run locally when GitHub Actions is unavailable. This is the
# formalization of the ad-hoc gate used to ship v0.20.0 through the
# 2026-08-06 Actions outage (issue #4671); the fallback *procedure* --
# including the operator sign-off requirement -- is documented in
# RELEASING.md ("Actions outage fallback").
#
# The job manifest below is an explicit list, NOT generated from ci.yml
# (job steps embed container/matrix/conditional logic with no local
# analogue). Drift is guarded by tests/test_local_gate_manifest.py, which
# asserts that `--list` output matches the ci.yml top-level job-id set --
# adding a job to ci.yml without updating this manifest fails the Test job.
#
# Usage:
#   scripts/ci/local-gate.sh [MODE] [JOB...]
#
# Modes (mutually exclusive; default is --cheap):
#   --cheap      Run the cheap gates only (lint, typecheck, test,
#                cpp-build-check, kicad-cli-smoke, routed-pcb-drc-check).
#                This is the default when no mode/jobs are given.
#   --fast       Alias for --cheap.
#   --full       Run everything: cheap gates + the long board jobs
#                (multiple hours; each board job re-routes a board from
#                scratch).
#   --all        Alias for --full.
#   --release    Cheap gates + the release extras (board-03 routing
#                baseline test + changelog gap report) -- the exact set
#                used to gate the v0.20.0 release during the outage.
#   --list       Print the manifest job ids (one per line) and exit.
#                Output is the drift-test contract: exactly the ci.yml
#                job ids, in ci.yml order. Release extras are NOT listed
#                (they are not ci.yml jobs).
#   JOB...       Run only the named jobs. Any id from --list plus the
#                release-extra ids (release-board03-baseline,
#                release-changelog-gap) is accepted.
#
# Exit code: non-zero iff any non-advisory selected job FAILs. Advisory
# jobs (diffpair/matchgroup routing regressions -- advisory in CI too) and
# SKIPped jobs (e.g. kicad-cli absent) never flip the exit code.
#
# Local-vs-CI parity notes:
#   * CI runs the test / smoke / board jobs inside the kicad/kicad:10.0
#     container. Locally you need kicad-cli (KiCad 10) on PATH for full
#     parity; jobs that strictly require it SKIP with a notice when it is
#     absent.
#   * Board jobs 03/04/06/07 stage the freshly-routed PCB over the
#     committed boards/*/output/ path (check_routed_drc.py keys its
#     allowlist/mfr override off the repo-relative path). CI runs in a
#     throwaway checkout; locally this script backs up the committed file
#     first and restores it afterwards -- pass, fail, or Ctrl-C (the restore
#     runs from an EXIT/INT/TERM trap scoped to the staged window).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Job manifest -- MUST stay in sync with the top-level job ids in
# .github/workflows/ci.yml (drift-guarded by tests/test_local_gate_manifest.py).
# Order matches ci.yml declaration order.
# ---------------------------------------------------------------------------
CHEAP_JOBS=(
  lint
  typecheck
  test
  cpp-build-check
  kicad-cli-smoke
  routed-pcb-drc-check
)

LONG_JOBS=(
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
# diffpair-routing-regression / matchgroup-routing-regression jobs).
# Mirrored here: they report FAIL but never flip the gate's exit code.
ADVISORY_JOBS=(
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
SKIP_RC=75

# ---------------------------------------------------------------------------
# Job implementations. Each job_<id> function runs in a subshell with
# `set -euo pipefail`; return 0 = PASS, $SKIP_RC = SKIP, anything else = FAIL.
# Commands mirror the corresponding ci.yml job's run steps (checkout /
# uv-install / container boilerplate excluded).
# ---------------------------------------------------------------------------

job_lint() {
  uv run ruff format . --check
  uv run ruff check .
}

job_typecheck() {
  uv run python scripts/ci/check_mypy_baseline.py
}

job_test() {
  uv run kct build-native
  uv run pytest -n auto -o addopts= --benchmark-disable --timeout=60 -m "not slow"
}

job_cpp_build_check() {
  bash scripts/build-cpp.sh clean
  bash scripts/build-cpp.sh build
  uv run python - <<'PYEOF'
from kicad_tools.router.cpp_backend import (
    is_cpp_available,
    get_cpp_unavailable_reason,
    _REQUIRED_CPP_BUILD_VERSION,
    router_cpp,
)
if not is_cpp_available():
    raise SystemExit(
        f"C++ backend unavailable after clean rebuild: "
        f"{get_cpp_unavailable_reason()}"
    )
actual = getattr(router_cpp, "BUILD_VERSION", None)
if actual != _REQUIRED_CPP_BUILD_VERSION:
    raise SystemExit(
        f"BUILD_VERSION mismatch after clean rebuild: "
        f"compiled={actual!r} required={_REQUIRED_CPP_BUILD_VERSION!r}. "
        f"Did you forget to bump ROUTER_CPP_BUILD_VERSION in both "
        f"cpp/include/types.hpp and cpp_backend.py?"
    )
print(f"C++ backend OK: BUILD_VERSION={actual}")
PYEOF
  uv run pytest tests/test_router_cpp_backend.py::TestCppBuildVersionGuard -v --no-cov
}

job_kicad_cli_smoke() {
  if ! command -v kicad-cli >/dev/null 2>&1; then
    echo "kicad-cli not found on PATH -- skipping (CI runs this in the kicad/kicad:10.0 container)"
    return "$SKIP_RC"
  fi
  kicad-cli --version
  uv run pytest tests/test_kicad_cli_roundtrip.py -v --no-cov
}

job_routed_pcb_drc_check() {
  # Local equivalent of the in-job diff detection: diff against the
  # merge-base with origin/main (mirrors the PR base-sha diff in CI).
  local base
  base="$(git merge-base origin/main HEAD 2>/dev/null || true)"
  if [ -z "$base" ]; then
    base="origin/main"
  fi
  echo "Diffing against base: $base"
  local files=()
  while IFS= read -r f; do
    [ -n "$f" ] && files+=("$f")
  done < <(
    git diff --name-only --diff-filter=AM "$base"...HEAD \
      | grep -E '^boards/[^/]+/output/[^/]+_routed\.kicad_pcb$' || true
  )
  if [ ${#files[@]} -eq 0 ]; then
    echo "No routed PCBs modified -- nothing to check (green in CI too)."
    return 0
  fi
  echo "Modified routed PCBs:"
  printf '  %s\n' "${files[@]}"
  uv run python scripts/ci/check_routed_drc.py "${files[@]}"
}

job_diffpair_routing_regression() {
  uv run kct build-native
  PYTHONHASHSEED=42 PYTHONUNBUFFERED=1 \
    uv run python scripts/ci/check_diffpair_coverage.py \
    boards/06-diffpair-test \
    --seed 42
}

job_matchgroup_routing_regression() {
  uv run kct build-native
  PYTHONHASHSEED=42 PYTHONUNBUFFERED=1 \
    uv run python scripts/ci/check_matchgroup_coverage.py \
    boards/07-matchgroup-test \
    --seed 42
}

# Idempotent restore of a staged-over committed file (see below). Safe to call
# twice: the backup is removed on the first successful restore.
_restore_committed_from_backup() {
  local bak=$1 committed=$2
  [ -f "$bak" ] || return 0
  cp "$bak" "$committed"
  rm -f "$bak"
}

# Helper for the DRC re-check steps in board jobs 03/04/06/07: those gates
# stage the fresh routed PCB over the committed path (check_routed_drc.py
# keys allowlist/mfr overrides off the repo-relative path). CI's checkout is
# throwaway; locally we back up and restore the committed file, pass or fail
# -- including on Ctrl-C, via a trap scoped to the staged window.
_check_drc_over_committed_path() {
  local fresh=$1 committed=$2
  local bak
  bak="$(mktemp "${TMPDIR:-/tmp}/local-gate-drc-bak.XXXXXX")"
  cp "$committed" "$bak"
  cp "$fresh" "$committed"
  local rc=0
  # The traps live in a nested subshell so they cover only the window in
  # which the committed file is staged over (an EXIT trap set directly in
  # this function would also fire at job-subshell exit, long after the
  # restore). INT/TERM restore and then re-raise the signal on the subshell
  # itself (after clearing the traps) so the caller still sees a
  # killed-by-signal child -- i.e. Ctrl-C keeps aborting the run instead of
  # being reported as a pass.
  (
    trap '_restore_committed_from_backup "$bak" "$committed"' EXIT
    trap 'trap - INT EXIT; _restore_committed_from_backup "$bak" "$committed"; kill -INT "$BASHPID"' INT
    trap 'trap - TERM EXIT; _restore_committed_from_backup "$bak" "$committed"; kill -TERM "$BASHPID"' TERM
    uv run python scripts/ci/check_routed_drc.py "$committed"
  ) || rc=$?
  # Belt-and-braces: if the subshell died without running its traps (SIGKILL),
  # restore here. No-op on the normal path.
  _restore_committed_from_backup "$bak" "$committed"
  return "$rc"
}

job_board_00_end_to_end() {
  uv run kct build-native
  rm -rf /tmp/board00-ci
  mkdir -p /tmp/board00-ci
  uv run python boards/00-simple-led/generate_design.py /tmp/board00-ci
  uv run kct check /tmp/board00-ci/simple_led_routed.kicad_pcb \
    | tee /tmp/board00-ci/check.out
  grep -qE '^Overall:[[:space:]]+PASSED' /tmp/board00-ci/check.out
  rm -rf /tmp/board00-staging
  mkdir -p /tmp/board00-staging/00-simple-led/output
  cp -r /tmp/board00-ci/. /tmp/board00-staging/00-simple-led/output/
  uv run kct board-metrics /tmp/board00-staging/00-simple-led
  uv run python scripts/ci/check_net_status.py \
    /tmp/board00-ci/simple_led_routed.kicad_pcb
  uv run python scripts/ci/check_board_00_e2e.py \
    /tmp/board00-staging/00-simple-led
}

job_board_01_end_to_end() {
  uv run kct build-native
  rm -rf /tmp/board01-ci
  mkdir -p /tmp/board01-ci
  uv run python boards/01-voltage-divider/generate_design.py /tmp/board01-ci
  uv run kct check /tmp/board01-ci/voltage_divider_routed.kicad_pcb \
    | tee /tmp/board01-ci/check.out
  grep -qE '^Overall:[[:space:]]+PASSED' /tmp/board01-ci/check.out
  rm -rf /tmp/board01-staging
  mkdir -p /tmp/board01-staging/01-voltage-divider/output
  cp -r /tmp/board01-ci/. /tmp/board01-staging/01-voltage-divider/output/
  uv run kct board-metrics /tmp/board01-staging/01-voltage-divider
  uv run python scripts/ci/check_net_status.py \
    /tmp/board01-ci/voltage_divider_routed.kicad_pcb
  uv run python scripts/ci/check_board_00_e2e.py \
    --stem voltage_divider \
    /tmp/board01-staging/01-voltage-divider
}

job_board_02_end_to_end() {
  uv run kct build-native
  rm -rf /tmp/board02-ci
  mkdir -p /tmp/board02-ci
  uv run python boards/02-charlieplex-led/generate_design.py /tmp/board02-ci
  uv run kct check /tmp/board02-ci/charlieplex_3x3_routed.kicad_pcb \
    | tee /tmp/board02-ci/check.out
  grep -qE '^Overall:[[:space:]]+PASSED' /tmp/board02-ci/check.out
  rm -rf /tmp/board02-staging
  mkdir -p /tmp/board02-staging/02-charlieplex-led/output
  cp -r /tmp/board02-ci/. /tmp/board02-staging/02-charlieplex-led/output/
  uv run kct board-metrics /tmp/board02-staging/02-charlieplex-led
  uv run python scripts/ci/check_net_status.py \
    /tmp/board02-ci/charlieplex_3x3_routed.kicad_pcb
  uv run python scripts/ci/check_board_00_e2e.py \
    --stem charlieplex_3x3 \
    /tmp/board02-staging/02-charlieplex-led
}

job_board_03_end_to_end() {
  uv run kct build-native
  rm -rf /tmp/board03-ci
  mkdir -p /tmp/board03-ci
  uv run python boards/03-usb-joystick/generate_design.py /tmp/board03-ci
  test -f /tmp/board03-ci/lvs.json
  test -f /tmp/board03-ci/usb_joystick.kicad_sch
  test -f /tmp/board03-ci/usb_joystick_routed.kicad_pcb
  uv run python -m kicad_tools.lvs.copper_lvs \
    /tmp/board03-ci/usb_joystick.kicad_sch \
    /tmp/board03-ci/usb_joystick_routed.kicad_pcb \
    > /tmp/board03-copper.json
  uv run python scripts/ci/check_copper_lvs.py /tmp/board03-copper.json
  _check_drc_over_committed_path \
    /tmp/board03-ci/usb_joystick_routed.kicad_pcb \
    boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb
  rm -rf /tmp/board03-staging
  mkdir -p /tmp/board03-staging/03-usb-joystick/output
  cp -r /tmp/board03-ci/. /tmp/board03-staging/03-usb-joystick/output/
  uv run kct board-metrics /tmp/board03-staging/03-usb-joystick
  uv run python scripts/ci/check_net_status.py \
    /tmp/board03-ci/usb_joystick_routed.kicad_pcb
  uv run python scripts/ci/check_board_00_e2e.py \
    --stem usb_joystick \
    --lvs-only \
    /tmp/board03-staging/03-usb-joystick
}

job_board_06_end_to_end() {
  uv run kct build-native
  rm -rf /tmp/board06-ci
  mkdir -p /tmp/board06-ci
  uv run python boards/06-diffpair-test/generate_design.py \
    /tmp/board06-ci --step all --seed 42 || true
  test -f /tmp/board06-ci/lvs.json
  test -f /tmp/board06-ci/diffpair_test.kicad_sch
  test -f /tmp/board06-ci/diffpair_test_routed.kicad_pcb
  uv run python -m kicad_tools.lvs.copper_lvs \
    /tmp/board06-ci/diffpair_test.kicad_sch \
    /tmp/board06-ci/diffpair_test_routed.kicad_pcb \
    > /tmp/board06-copper.json
  uv run python scripts/ci/check_copper_lvs.py /tmp/board06-copper.json
  _check_drc_over_committed_path \
    /tmp/board06-ci/diffpair_test_routed.kicad_pcb \
    boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb
  rm -rf /tmp/board06-staging
  mkdir -p /tmp/board06-staging/06-diffpair-test/output
  cp -r /tmp/board06-ci/. /tmp/board06-staging/06-diffpair-test/output/
  uv run kct board-metrics /tmp/board06-staging/06-diffpair-test
  uv run python scripts/ci/check_net_status.py \
    /tmp/board06-ci/diffpair_test_routed.kicad_pcb
  uv run python scripts/ci/check_board_00_e2e.py \
    --stem diffpair_test \
    --lvs-only \
    /tmp/board06-staging/06-diffpair-test
}

job_board_07_end_to_end() {
  local known_opens="DQ3,DQ4,MIPI_DAT0_N,TMDS_D0_N,TMDS_D1_N"
  uv run kct build-native
  rm -rf /tmp/board07-ci
  mkdir -p /tmp/board07-ci
  uv run python boards/07-matchgroup-test/generate_design.py \
    /tmp/board07-ci --step all --seed 42 || true
  test -f /tmp/board07-ci/lvs.json
  test -f /tmp/board07-ci/matchgroup_test.kicad_sch
  test -f /tmp/board07-ci/matchgroup_test_routed.kicad_pcb
  uv run python -m kicad_tools.lvs.copper_lvs \
    /tmp/board07-ci/matchgroup_test.kicad_sch \
    /tmp/board07-ci/matchgroup_test_routed.kicad_pcb \
    > /tmp/board07-copper.json
  uv run python scripts/ci/check_copper_lvs.py \
    --expect-opens "$known_opens" \
    /tmp/board07-copper.json
  _check_drc_over_committed_path \
    /tmp/board07-ci/matchgroup_test_routed.kicad_pcb \
    boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb
  rm -rf /tmp/board07-staging
  mkdir -p /tmp/board07-staging/07-matchgroup-test/output
  cp -r /tmp/board07-ci/. /tmp/board07-staging/07-matchgroup-test/output/
  uv run kct board-metrics /tmp/board07-staging/07-matchgroup-test
  uv run python scripts/ci/check_net_status.py \
    --known-open-nets "$known_opens" \
    /tmp/board07-ci/matchgroup_test_routed.kicad_pcb
  uv run python scripts/ci/check_board_00_e2e.py \
    --stem matchgroup_test \
    --lvs-known-opens "$known_opens" \
    /tmp/board07-staging/07-matchgroup-test
}

job_board_05_routing_regression() {
  uv run kct build-native
  rm -rf /tmp/board05-ci
  mkdir -p /tmp/board05-ci
  uv run python boards/05-bldc-motor-controller/design.py \
    /tmp/board05-ci || true
  test -f /tmp/board05-ci/bldc_controller_routed.kicad_pcb
  uv run python scripts/ci/check_board_05_blocking.py \
    --max-blocking 7 \
    /tmp/board05-ci/bldc_controller_routed.kicad_pcb
}

job_board_04_end_to_end() {
  uv run kct build-native
  rm -rf /tmp/board04-ci
  mkdir -p /tmp/board04-ci
  uv run python boards/04-stm32-devboard/generate_design.py \
    /tmp/board04-ci || true
  test -f /tmp/board04-ci/lvs.json
  test -f /tmp/board04-ci/stm32_devboard_routed.kicad_pcb
  _check_drc_over_committed_path \
    /tmp/board04-ci/stm32_devboard_routed.kicad_pcb \
    boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb
  rm -rf /tmp/board04-staging
  mkdir -p /tmp/board04-staging/04-stm32-devboard/output
  cp -r /tmp/board04-ci/. /tmp/board04-staging/04-stm32-devboard/output/
  uv run kct board-metrics /tmp/board04-staging/04-stm32-devboard
  uv run python scripts/ci/check_net_status.py \
    /tmp/board04-ci/stm32_devboard_routed.kicad_pcb
  uv run python scripts/ci/check_board_00_e2e.py \
    --stem stm32_devboard \
    --lvs-only \
    /tmp/board04-staging/04-stm32-devboard
}

# Release extras (not ci.yml jobs; see RELEASING.md "Actions outage fallback").
job_release_board03_baseline() {
  uv run pytest tests/router/test_board03_routing_baseline.py
}

job_release_changelog_gap() {
  uv run python scripts/changelog_gap_report.py
}

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
  fn="job_${job//-/_}"
  echo "=============================================================="
  echo "=== ${job}"
  echo "=============================================================="
  start=$SECONDS
  rc=0
  # LOAD-BEARING: do NOT "simplify" this back to `("$fn") || rc=$?`.
  # Bash disables errexit inside any compound command that is an operand of
  # `||` / `&&` / `if`, and that suppression is inherited by the subshell and
  # every function it calls -- so only a job's LAST command would decide
  # PASS/FAIL (a failing `ruff format --check` followed by a passing
  # `ruff check` reported PASS). Running the subshell as its own statement,
  # with an explicit `set -e` inside it (the outer `set +e` is inherited),
  # keeps errexit live for every command in the job. The SKIP sentinel still
  # works: `return $SKIP_RC` exits the subshell with 75.
  # Regression-guarded by tests/test_local_gate_manifest.py.
  set +e
  (
    set -e
    "$fn"
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
