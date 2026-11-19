#!/usr/bin/env bash
# scripts/ci/jobs/board-05-routing-regression.sh -- board 05 ("bldc-motor-
# controller") blocking-net baseline gate (Issue #3822). The single copy of
# the `board-05-routing-regression` job's check commands; both
# .github/workflows/ci.yml's job and scripts/ci/local-gate.sh run this file
# (2am#7561 ruling). The jlcparts catalog cache/sync steps are CI-only
# (cache plumbing + network) and stay as plain workflow steps.
#
# THE GATE IS ZERO (issue #7547): a fresh CI regen of board 05 measured
# blocking_incomplete_count = 0 in every main run that reached the gate, so
# ANY blocking incomplete net fails (the previous bound of 7 would have
# hidden up to 7 regressions). If a net must be tolerated again, list it by
# name with an open tracker in scripts/ci/check_board_05_blocking.py's
# identity-keyed mechanism (#7359) -- never restore a bare count.
#
# CI-VALIDATED ONLY: a local host may route board 05 to more blocking nets
# than the Linux CI router (the documented host-vs-CI divergence, #3822).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"

step "Build C++ router backend"
# REQUIRED: the recipe re-routes during regen (see repo CLAUDE.md).
uv run kct build-native

step "Regenerate + route board 05 from recipe (clean tmp directory)"
# Board 05's recipe is design.py (NOT generate_design.py) and takes only an
# output-dir positional arg. It routes PARTIAL by design and may exit
# non-zero, so tolerate that; the blocking-net gate below is the assertion.
rm -rf /tmp/board05-ci
mkdir -p /tmp/board05-ci
uv run python boards/05-bldc-motor-controller/design.py \
  /tmp/board05-ci || true
test -f /tmp/board05-ci/bldc_controller_routed.kicad_pcb

step "Assert blocking-net baseline (== 0)"
# Hard gate: exits 2 if blocking_incomplete_count > 0.
uv run python scripts/ci/check_board_05_blocking.py \
  --max-blocking 0 \
  /tmp/board05-ci/bldc_controller_routed.kicad_pcb
