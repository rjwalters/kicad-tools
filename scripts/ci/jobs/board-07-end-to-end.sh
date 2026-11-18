#!/usr/bin/env bash
# scripts/ci/jobs/board-07-end-to-end.sh -- board 07 ("matchgroup-test")
# end-to-end LVS-clean gate (Issue #3779). The single copy of the
# `board-07-end-to-end` job's check commands; both .github/workflows/ci.yml's
# job and scripts/ci/local-gate.sh run this file (2am#7561 ruling). The
# failed-routing-bundle collection/upload steps are CI-only (if: failure())
# and stay as plain workflow steps; the job's CI-side
# `vars.BOARD_07_CI_ENABLED` gate stays on the workflow job.
#
# The fixture schematic is FULLY WIRED (#4012: 244/244 pads bound) and, since
# #7557, every signal net routes (31/31), so this is a plain LVS-clean gate:
# copper-LVS re-check (no flags; any open, short or vacuous verdict fails),
# the synthetic routing gate on the regenerated bundle, plane-net completion
# with NO known-open exclusions, and the --lvs-only asserter. NEVER
# reintroduce a known-opens list to absorb a regression (guarded by
# tests/test_board07_known_opens_sync.py).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"

step "Build C++ router backend"
uv run kct build-native

step "Regenerate board 07 from recipe (--step all, clean tmp directory)"
# The recipe still exits non-zero on its allowlisted DRC residuals
# (skew/impedance, #7550/#7349/#7619) -- tolerate it; the asserters below
# are the gate.
rm -rf /tmp/board07-ci
mkdir -p /tmp/board07-ci
uv run python boards/07-matchgroup-test/generate_design.py \
  /tmp/board07-ci --step all --seed 42 || true
test -f /tmp/board07-ci/lvs.json
test -f /tmp/board07-ci/matchgroup_test.kicad_sch
test -f /tmp/board07-ci/matchgroup_test_routed.kicad_pcb

step "Copper-LVS clean re-check (out-of-process, #3838/#7557)"
uv run python -m kicad_tools.lvs.copper_lvs \
  /tmp/board07-ci/matchgroup_test.kicad_sch \
  /tmp/board07-ci/matchgroup_test_routed.kicad_pcb \
  > /tmp/board07-copper.json
uv run python scripts/ci/check_copper_lvs.py \
  /tmp/board07-copper.json

step "Independent synthetic routing gate (matching PCB and sidecars)"
mkdir -p boards/07-matchgroup-test/regression-output
cp -a /tmp/board07-ci/. boards/07-matchgroup-test/regression-output/
uv run python scripts/ci/check_matchgroup_coverage.py \
  boards/07-matchgroup-test --skip-route --seed 42

step "Independent DRC re-check within allowlist (out-of-process, #3840)"
check_drc_over_committed_path \
  /tmp/board07-ci/matchgroup_test_routed.kicad_pcb \
  boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb

step "Emit board.json via kct board-metrics"
rm -rf /tmp/board07-staging
mkdir -p /tmp/board07-staging/07-matchgroup-test/output
cp -r /tmp/board07-ci/. /tmp/board07-staging/07-matchgroup-test/output/
uv run kct board-metrics /tmp/board07-staging/07-matchgroup-test

step "Assert net completion (#4531, no known opens since #7557)"
uv run python scripts/ci/check_net_status.py \
  /tmp/board07-ci/matchgroup_test_routed.kicad_pcb

step "Assert artifacts + lvs.json clean (LVS-only, #7557)"
uv run python scripts/ci/check_board_00_e2e.py \
  --stem matchgroup_test \
  --lvs-only \
  /tmp/board07-staging/07-matchgroup-test
