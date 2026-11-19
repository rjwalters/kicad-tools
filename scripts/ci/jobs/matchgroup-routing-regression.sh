#!/usr/bin/env bash
# scripts/ci/jobs/matchgroup-routing-regression.sh -- board 07 match-group
# coverage gate (Issue #2726 / Epic #2661 Phase 3N). The single copy of the
# `matchgroup-routing-regression` job's re-route + check command; both
# .github/workflows/ci.yml's job and scripts/ci/local-gate.sh run this file
# (2am#7561 ruling). The failed-routing-bundle collection/upload steps are
# CI-only (if: failure()) and stay as plain workflow steps; the job's CI-side
# `vars.BOARD_07_CI_ENABLED` gate stays on the workflow job.
#
# Re-routes board 07 (the N-trace match-group testbench) from scratch and
# verifies that every blocking DRC finding is identity-allowlisted and still
# fires, and that the match_group_length_skew rule was actually exercised
# against a declared group (the sidecar net-class map threads through
# check_matchgroup_coverage.py per the #2692 pattern).
#
# Determinism: --seed 42 (Issue #2589), PYTHONHASHSEED pinned at the step
# level (Issue #3146) and PYTHONUNBUFFERED=1.
#
# Usage: matchgroup-routing-regression.sh [BOARD-DIR]
#   default: boards/07-matchgroup-test (the workflow passes its matrix.board).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

board="${1:-boards/07-matchgroup-test}"

step "Build C++ router backend"
# `uv sync` does NOT build the native extension (repo CLAUDE.md); without it
# board 07 routing falls back to pure Python (10-100x slower), the dominant
# source of the cross-platform DRC variance in PR #3145.
uv run kct build-native

step "Re-route board + check match-group coverage"
export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"
uv run python scripts/ci/check_matchgroup_coverage.py "$board" --seed 42
