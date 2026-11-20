#!/usr/bin/env bash
# scripts/ci/jobs/board-01-end-to-end.sh -- board 01 ("voltage-divider")
# end-to-end gate (Issue #3762). The single copy of the `board-01-end-to-end`
# job's check commands; both .github/workflows/ci.yml's job and
# scripts/ci/local-gate.sh run this file (2am#7561 ruling). Same gate shape as
# board 00 (see board-00-end-to-end.sh), parametrised by the generalized
# asserter's --stem: board 01 is verified clean on both the copper-extracted
# and label-based LVS comparators (write_lvs_report require_clean=True).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Build C++ router backend"
uv run kct build-native

step "Regenerate board 01 from recipe (clean tmp directory)"
# The recipe exits non-zero if ERC/DRC/LVS fail OR write_lvs_report raises
# BoardNetlistMismatch -- board 01 hard-gates on both comparators.
rm -rf /tmp/board01-ci
mkdir -p /tmp/board01-ci
uv run python boards/01-voltage-divider/generate_design.py /tmp/board01-ci

step "Assert kct check Overall PASSED on regenerated routed PCB"
uv run kct check /tmp/board01-ci/voltage_divider_routed.kicad_pcb \
  | tee /tmp/board01-ci/check.out
grep -qE '^Overall:[[:space:]]+PASSED' /tmp/board01-ci/check.out

step "Emit board.json via kct board-metrics"
rm -rf /tmp/board01-staging
mkdir -p /tmp/board01-staging/01-voltage-divider/output
cp -r /tmp/board01-ci/. /tmp/board01-staging/01-voltage-divider/output/
uv run kct board-metrics /tmp/board01-staging/01-voltage-divider

step "Assert plane-net completion (no floating pads, #4531)"
uv run python scripts/ci/check_net_status.py \
  /tmp/board01-ci/voltage_divider_routed.kicad_pcb

step "Assert artifacts + lvs.json + board.json fields"
uv run python scripts/ci/check_board_00_e2e.py \
  --stem voltage_divider \
  --routing-only \
  /tmp/board01-staging/01-voltage-divider
