#!/usr/bin/env bash
# scripts/ci/jobs/board-04-end-to-end.sh -- board 04 ("stm32-devboard")
# LVS-only + DRC-allowlist end-to-end gate (Issue #3839). The single copy of
# the `board-04-end-to-end` job's check commands; both
# .github/workflows/ci.yml's job and scripts/ci/local-gate.sh run this file
# (2am#7561 ruling).
#
# The fresh regen is staged over boards/04-stm32-devboard/output/ as a WHOLE
# (not just the PCB): the reviewed paid-drill validator dispatched by
# check_routed_drc_internal.py checks matching constraints and manifest
# freshness as well as copper, so the fresh bundle must travel with its
# mtimes instead of mixing a new PCB with old evidence. The staging helper
# backs the committed directory up and restores it afterwards -- a no-op in
# CI's throwaway checkout, and what keeps the local working tree clean.
#
# RECIPE EXIT IS NOT GATED (|| true): board 04's route is subject to the
# documented host-vs-CI router divergence (#3822) and may land a PARTIAL
# route even though the artifact is DRC-within-allowlist + copper-LVS clean;
# the out-of-process asserters below are the real gate. Since #4408 the
# recipe reproduces a strict-0 board, so the DRC gate runs with default
# (strict-0) tolerance.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"

step "Build C++ router backend"
# REQUIRED: the recipe re-routes during regen (see repo CLAUDE.md).
uv run kct build-native

step "Regenerate board 04 from recipe (clean tmp directory)"
# Board 04's recipe takes only an output-dir positional arg (no --step /
# --seed). write_lvs_report(require_clean=True) still RAISES on an actual
# copper short/open, so a genuine LVS regression aborts here.
rm -rf /tmp/board04-ci
mkdir -p /tmp/board04-ci
uv run python boards/04-stm32-devboard/generate_design.py \
  /tmp/board04-ci || true
test -f /tmp/board04-ci/lvs.json
test -f /tmp/board04-ci/stm32_devboard_routed.kicad_pcb

step "Assert fresh DRC strict-0 (recipe-vs-artifact now converged)"
# Stage the whole fresh bundle over the committed output/ path (paid-drill
# validator requires fresh manifest evidence, see header) and gate strict-0:
# ANY blocking error -- a re-introduced hole-to-hole pair or a new rule --
# fails (exit 2).
stage_over_committed_dir /tmp/board04-ci boards/04-stm32-devboard/output \
  uv run python scripts/ci/check_routed_drc_internal.py \
  boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb

step "Emit board.json via kct board-metrics"
rm -rf /tmp/board04-staging
mkdir -p /tmp/board04-staging/04-stm32-devboard/output
cp -r /tmp/board04-ci/. /tmp/board04-staging/04-stm32-devboard/output/
uv run kct board-metrics /tmp/board04-staging/04-stm32-devboard

step "Assert plane-net completion (no floating pads, #4531)"
uv run python scripts/ci/check_net_status.py \
  /tmp/board04-ci/stm32_devboard_routed.kicad_pcb

step "Assert artifacts + lvs.json clean (LVS-only)"
# No status/drc_violations assertions: board 04 carries 2 allowlisted drills
# + 1 advisory connectivity finding -> status='partial'.
uv run python scripts/ci/check_board_00_e2e.py \
  --stem stm32_devboard \
  --lvs-only \
  /tmp/board04-staging/04-stm32-devboard
