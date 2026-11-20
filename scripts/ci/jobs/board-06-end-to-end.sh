#!/usr/bin/env bash
# scripts/ci/jobs/board-06-end-to-end.sh -- board 06 ("diffpair-test")
# end-to-end LVS gate (Issue #3779, LVS re-enabled by #4012). The single copy
# of the `board-06-end-to-end` job's check commands; both
# .github/workflows/ci.yml's job and scripts/ci/local-gate.sh run this file
# (2am#7561 ruling). The failed-routing-bundle collection/upload steps are
# CI-only (if: failure()) and stay as plain workflow steps.
#
# The fixture schematic is FULLY WIRED (#4012: 198/198 pads bound), so both
# LVS comparators carry real evidence; the recipe hard-gates on both and
# emits lvs.json. Gates:
#   * copper-LVS clean re-check, out-of-process (any open/short or vacuous
#     verdict fails -- the #4006 guard forces clean=false when zero pins
#     bind);
#   * the synthetic routing gate on the regenerated bundle copied into the
#     git-ignored regression-output/ directory (matching PCB and sidecars;
#     historical pair/group baselines never permit new physical errors);
#   * plane-net completion (raw unconnected-pad gate, #4531);
#   * artifacts + lvs.json clean via the --lvs-only asserter.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"

step "Build C++ router backend"
uv run kct build-native

step "Regenerate board 06 from recipe (--step all, clean tmp directory)"
# The recipe hard-gates on LVS (#4012), so a copper open/short or label
# divergence exits non-zero -- tolerate the exit code and let the explicit
# asserters below be the gate (a dirty regen leaves lvs.json with
# clean=false, which the --lvs-only asserter rejects; an aborted recipe also
# fails the manifest presence check).
rm -rf /tmp/board06-ci
mkdir -p /tmp/board06-ci
uv run python boards/06-diffpair-test/generate_design.py \
  /tmp/board06-ci --step all --seed 42 || true
test -f /tmp/board06-ci/lvs.json
test -f /tmp/board06-ci/diffpair_test.kicad_sch
test -f /tmp/board06-ci/diffpair_test_routed.kicad_pcb

step "Copper-LVS clean re-check (out-of-process, #3838/#4012)"
uv run python -m kicad_tools.lvs.copper_lvs \
  /tmp/board06-ci/diffpair_test.kicad_sch \
  /tmp/board06-ci/diffpair_test_routed.kicad_pcb \
  > /tmp/board06-copper.json
uv run python scripts/ci/check_copper_lvs.py /tmp/board06-copper.json

step "Independent synthetic routing gate (matching PCB and sidecars)"
# Preserve the regenerated bundle's routing context in the git-ignored
# regression-output/ directory; the active release has a different circuit
# and must retain its zero-error acceptance.
mkdir -p boards/06-diffpair-test/regression-output
cp -a /tmp/board06-ci/. boards/06-diffpair-test/regression-output/
uv run python scripts/ci/check_diffpair_coverage.py \
  boards/06-diffpair-test --skip-route --seed 42

# NOTE: deliberately NO converged DRC re-check here, unlike boards 03/04.
# CI's board-06 job never ran one on main, and converging it turned the gate
# red (46 blocking errors from check_routed_drc_internal.py on the fresh
# PCB; PR #9392 review, finding 2). The residuals are #6396/#8839/#8842
# territory; convergence re-lands deliberately via the tracking issue, not
# silently. (Main's local-gate --full ran this step for board 06 and fails
# the same way -- that latent failure is filed, not silently dropped.)
step "Emit board.json via kct board-metrics"
rm -rf /tmp/board06-staging
mkdir -p /tmp/board06-staging/06-diffpair-test/output
cp -r /tmp/board06-ci/. /tmp/board06-staging/06-diffpair-test/output/
uv run kct board-metrics /tmp/board06-staging/06-diffpair-test

step "Assert plane-net completion (no floating pads, #4531)"
uv run python scripts/ci/check_net_status.py \
  /tmp/board06-ci/diffpair_test_routed.kicad_pcb

step "Assert artifacts + LVS clean (#4012)"
uv run python scripts/ci/check_board_00_e2e.py \
  --stem diffpair_test \
  --lvs-only \
  /tmp/board06-staging/06-diffpair-test
