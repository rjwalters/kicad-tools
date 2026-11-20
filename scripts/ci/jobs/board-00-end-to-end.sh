#!/usr/bin/env bash
# scripts/ci/jobs/board-00-end-to-end.sh -- board 00 ("simple-led") end-to-end
# gate (Issue #3751). The single copy of the `board-00-end-to-end` job's check
# commands; both .github/workflows/ci.yml's job and scripts/ci/local-gate.sh
# run this file (2am#7561 ruling).
#
# Regenerates the board from its recipe into a clean tmp directory (never
# touching the committed boards/00-simple-led/output/ tree), then asserts:
#   1. `kct check` Overall: PASSED (DRC + ERC + LVS + Manifest, the #3750
#      meta contract);
#   2. board.json re-emitted via `kct board-metrics` (the demo-gallery
#      contract, #3749);
#   3. raw plane-net completion via check_net_status.py (no floating pads,
#      #4531);
#   4. artifacts + lvs.json + board.json fields via the asserter's
#      --routing-only mode (fresh output stays readiness=unverified /
#      status=partial; this gate does not manufacture a release verdict).
#
# Copper UUIDs, segment ordering and generator timestamps are
# nondeterministic, so the gate never byte-diffs against the committed tree.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Build C++ router backend"
# Per repo CLAUDE.md: without the native extension the recipe's routing step
# falls back to pure Python (10-100x slower).
uv run kct build-native

step "Regenerate board 00 from recipe (clean tmp directory)"
# The recipe exits non-zero if ERC/DRC/LVS/MFG fail OR run_lvs() raises
# BoardNetlistMismatch -- the polarity-class regression this gate catches.
rm -rf /tmp/board00-ci
mkdir -p /tmp/board00-ci
uv run python boards/00-simple-led/generate_design.py /tmp/board00-ci

step "Assert kct check Overall PASSED on regenerated routed PCB"
# tee surfaces the full output AND lets grep gate on the exact
# "Overall: PASSED" line (defence in depth against verdict-string renames).
uv run kct check /tmp/board00-ci/simple_led_routed.kicad_pcb \
  | tee /tmp/board00-ci/check.out
grep -qE '^Overall:[[:space:]]+PASSED' /tmp/board00-ci/check.out

step "Emit board.json via kct board-metrics"
# board.json is the public data contract emitted by `kct board-metrics`;
# point it at the standard <board_dir>/output/ staging shape.
rm -rf /tmp/board00-staging
mkdir -p /tmp/board00-staging/00-simple-led/output
cp -r /tmp/board00-ci/. /tmp/board00-staging/00-simple-led/output/
uv run kct board-metrics /tmp/board00-staging/00-simple-led

step "Assert plane-net completion (no floating pads, #4531)"
# Raw total_unconnected_pads gate: a floating power/plane pad passes
# DRC/LVS/router "N/N nets" but is electrically broken, and
# blocking_incomplete_count would name-exclude it.
uv run python scripts/ci/check_net_status.py \
  /tmp/board00-ci/simple_led_routed.kicad_pcb

step "Assert artifacts + lvs.json + board.json fields"
uv run python scripts/ci/check_board_00_e2e.py \
  --routing-only \
  /tmp/board00-staging/00-simple-led
