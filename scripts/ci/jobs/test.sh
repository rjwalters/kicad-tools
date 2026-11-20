#!/usr/bin/env bash
# scripts/ci/jobs/test.sh -- the `test` job's check commands.
# The single copy of the pytest invocations that both
# .github/workflows/ci.yml's `test` job and scripts/ci/local-gate.sh run
# (2am#7561 ruling). CI calls one sub-command per workflow step so each keeps
# its own native-observer wrapper, artifact retention and `if:` gating; the
# local gate runs the script with no argument, which executes every
# sub-command in CI order.
#
# Usage:
#   test.sh                      # everything (local-gate invocation)
#   test.sh conformance|board05|mask-copper|stitch|zones|bulk [extra args...]
#
# Extra arguments are forwarded verbatim to the underlying command, which is
# how CI adds artifact plumbing (--basetemp/--junitxml/--artifacts) without
# duplicating the test selection here.
#
# Module partitioning (Issues #5509/#5417/#5452/#5501): every item in the
# conformance corpus, the board-05 DRC regression and the native
# mask/stitch/zones acceptance modules launches its own kicad-cli, so they
# run serially OUTSIDE the xdist bulk pool; the bulk selection's matching
# --ignore flags below keep the suite from running twice.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

ensure_native_backend() {
  step "Build C++ router backend"
  # `uv sync` does NOT build the native extension (repo CLAUDE.md "Routing
  # performance: build the C++ backend first"). Without it, router tests fall
  # back to pure Python (10-100x slower) and the --timeout=60 reaper kills
  # tests that pass comfortably with the C++ backend.
  uv run kct build-native
}

run_conformance() {
  # Epic #5509 Phase 1c: no `-n`, the corpus is small (one item per seed, six
  # smoke seeds) and kicad-cli fan-out is exactly the thing being avoided.
  # --timeout=120 rather than the pool's 60 because a refilled item costs
  # three kicad-cli invocations. Skips itself without kicad-cli (module
  # skipif), exactly as in CI.
  step "Run clearance conformance corpus"
  uv run pytest tests/conformance -o addopts= --benchmark-disable --timeout=120 -m "not slow" "$@"
}

run_board05() {
  # The redesigned board has over 8,000 segments; run its single full DRC
  # report without competing xdist workers, retaining the 120-second
  # subprocess cap and every manufacturing assertion.
  step "Run Board05 manufacturing regression"
  uv run pytest tests/test_board_05_drc_allowlist.py -o addopts= --benchmark-disable --timeout=60 -m "not slow" "$@"
}

run_mask_copper() {
  step "Run native mask-to-copper gate"
  if [ "$#" -eq 0 ]; then
    # Local run: no CI artifact retention; diagnostics go to a scratch dir.
    set -- --artifacts "$(mktemp -d "${TMPDIR:-/tmp}/mask-copper-native.XXXXXX")"
  fi
  uv run --frozen python scripts/ci/check_mask_copper_native.py "$@"
}

run_stitch() {
  # Issue #5417: four concurrent KiCad refills exhausted CI's 12 GiB cgroup;
  # keep every physical assertion and the timeout, but no xdist pool.
  step "Run native physical-stitch acceptance"
  uv run pytest tests/test_stitch_physical_completion.py -o addopts= --benchmark-disable --timeout=60 -m "not slow" "$@"
}

run_zones() {
  # Issue #5452: native zone refills were OOM-killed while competing with
  # pytest workers; run these modules without that pool.
  step "Run native zone acceptance"
  uv run pytest tests/test_zones_cmd.py tests/test_zones_hv_keepout.py -o addopts= --benchmark-disable --timeout=60 -m "not slow" "$@"
}

run_bulk() {
  step "Run tests"
  test_args=(
    --ignore=tests/test_board_05_drc_allowlist.py
    --ignore=tests/test_mask_copper_native.py
    --ignore=tests/test_stitch_physical_completion.py
    --ignore=tests/test_zones_cmd.py
    --ignore=tests/test_zones_hv_keepout.py
    # Epic #5509 Phase 1c (#5515): the only ignore that is load-bearing for
    # *correctness* rather than only resource contention -- tests/conformance
    # has its own dedicated sub-command above, so without this the suite runs
    # twice.
    --ignore=tests/conformance
  )
  # Match the Board 07 jobs: gated on the repository variable, forwarded by
  # the workflow as BOARD_07_CI_ENABLED (unset locally -> ignored).
  if [[ "${BOARD_07_CI_ENABLED:-}" != "true" ]]; then
    test_args+=(--ignore=tests/test_board_07_matchgroup_test.py)
  fi
  uv run pytest -n auto -rfEsx -o addopts= --benchmark-disable --timeout=60 \
    -m "not slow and not ci_extended" --durations=30 "${test_args[@]}" "$@"
}

case "${1:-}" in
  "") ensure_native_backend; run_conformance; run_board05; run_mask_copper; run_stitch; run_zones; run_bulk ;;
  conformance) shift; ensure_native_backend; run_conformance "$@" ;;
  board05) shift; ensure_native_backend; run_board05 "$@" ;;
  mask-copper) shift; ensure_native_backend; run_mask_copper "$@" ;;
  stitch) shift; ensure_native_backend; run_stitch "$@" ;;
  zones) shift; ensure_native_backend; run_zones "$@" ;;
  bulk) shift; ensure_native_backend; run_bulk "$@" ;;
  *)
    echo "Unknown sub-command: ${1:-} (see the header of $0)" >&2
    exit 2
    ;;
esac
