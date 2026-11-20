#!/usr/bin/env bash
# scripts/ci/jobs/board-03-end-to-end.sh -- board 03 ("usb-joystick") LVS-only
# end-to-end gate (Issue #3795 / #3780 Part 2). The single copy of the
# `board-03-end-to-end` job's check commands; both .github/workflows/ci.yml's
# job and scripts/ci/local-gate.sh run this file (2am#7561 ruling). The
# failed-routing-bundle collection/upload steps are CI-only (if: failure())
# and stay as plain workflow steps.
#
# Uses the asserter's --lvs-only mode (NOT the full e2e gate): board 03
# carries 4 advisory via_in_pad DRC residuals, so board-metrics classifies it
# status:partial / drc_violations:4 and the full status:ok assertion would
# fail on them. --lvs-only asserts artifact presence + lvs.json clean=true
# (+ board.json lvs_clean=true), which is the meaningful copper-LVS gate.
# The fresh PCB's DRC is still validated out-of-process against the per-board
# allowlist, staged over the committed path (check_routed_drc_internal.py
# keys allowlist/mfr overrides off the repo-relative path); the staging
# helper backs the committed file up and restores it afterwards, which is a
# no-op in CI's throwaway checkout and keeps the local tree clean.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"

step "Build C++ router backend"
# REQUIRED: the recipe re-routes during regen, and per repo CLAUDE.md a
# missing native extension makes routing multi-minute-per-net.
uv run kct build-native

step "Regenerate board 03 from recipe (clean tmp directory)"
# Board 03's recipe takes only an output-dir argument and routes fully
# (no --step / --seed); write_lvs_report(require_clean=True) raises on any
# copper short/open.
rm -rf /tmp/board03-ci
mkdir -p /tmp/board03-ci
uv run python boards/03-usb-joystick/generate_design.py /tmp/board03-ci
test -f /tmp/board03-ci/lvs.json
test -f /tmp/board03-ci/usb_joystick.kicad_sch
test -f /tmp/board03-ci/usb_joystick_routed.kicad_pcb

step "Independent copper-LVS re-check (out-of-process, issue #3840)"
# Re-run compare_copper_netlist in a process the job controls (NOT the
# recipe) so a mis-written lvs.json cannot mask a real short/open.
uv run python -m kicad_tools.lvs.copper_lvs \
  /tmp/board03-ci/usb_joystick.kicad_sch \
  /tmp/board03-ci/usb_joystick_routed.kicad_pcb \
  > /tmp/board03-copper.json
uv run python scripts/ci/check_copper_lvs.py /tmp/board03-copper.json

step "Independent DRC re-check within allowlist (out-of-process, issue #3840)"
check_drc_over_committed_path \
  /tmp/board03-ci/usb_joystick_routed.kicad_pcb \
  boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb

step "Emit board.json via kct board-metrics"
rm -rf /tmp/board03-staging
mkdir -p /tmp/board03-staging/03-usb-joystick/output
cp -r /tmp/board03-ci/. /tmp/board03-staging/03-usb-joystick/output/
uv run kct board-metrics /tmp/board03-staging/03-usb-joystick

step "Assert plane-net completion (no floating pads, #4531)"
# THIS is the board from the #4531 repro: PR #4525 committed a board-03
# artifact with J2.1 (VCC) floating and all 16 CI checks passed.
uv run python scripts/ci/check_net_status.py \
  /tmp/board03-ci/usb_joystick_routed.kicad_pcb

step "Assert artifacts + lvs.json clean (LVS-only)"
uv run python scripts/ci/check_board_00_e2e.py \
  --stem usb_joystick \
  --lvs-only \
  /tmp/board03-staging/03-usb-joystick
