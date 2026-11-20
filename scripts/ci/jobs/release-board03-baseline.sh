#!/usr/bin/env bash
# scripts/ci/jobs/release-board03-baseline.sh -- release extra (NOT a ci.yml
# job): the board-03 routing baseline regression test. Part of the
# Actions-outage release gate (`local-gate.sh --release`); see RELEASING.md
# "Actions outage fallback".
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Board 03 routing baseline"
uv run pytest tests/router/test_board03_routing_baseline.py
