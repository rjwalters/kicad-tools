#!/usr/bin/env bash
# scripts/ci/jobs/diffpair-routing-regression.sh -- board 06 diff-pair coverage
# gate (Issue #2660 / Epic #2556 Phase 4N). The single copy of the
# `diffpair-routing-regression` job's re-route + check command; both
# .github/workflows/ci.yml's job and scripts/ci/local-gate.sh run this file
# (2am#7561 ruling). The failed-routing-bundle collection/upload steps are
# CI-only (if: failure()) and stay as plain workflow steps.
#
# Re-routes board 06 (the diff-pair testbench) from scratch and verifies
#   (a) every blocking DRC finding is listed by identity in the gate script
#       and still fires (count outside the allowance must be 0, #7367); AND
#   (b) each of the three diff-pair DRC rules was actually exercised against
#       an engaged pair (per-rule counter non-zero).
#
# Determinism: --seed 42 (Issue #2589), PYTHONHASHSEED pinned to match, and
# PYTHONUNBUFFERED=1 so a timeout is attributable to a specific pipeline step
# instead of going silent in a block-buffered pipe (the #3413 lesson).
#
# Usage: diffpair-routing-regression.sh [BOARD-DIR]
#   default: boards/06-diffpair-test (the workflow passes its matrix.board).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

board="${1:-boards/06-diffpair-test}"

step "Build C++ router backend"
# `uv sync` does NOT build the native extension (repo CLAUDE.md "Routing
# performance: build the C++ backend first"); without it board 06 routing
# falls back to pure Python (10-100x slower) and blows past the job timeout
# (Issue #3097).
uv run kct build-native

step "Re-route board + check diff-pair coverage"
export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"
uv run python scripts/ci/check_diffpair_coverage.py "$board" --seed 42
