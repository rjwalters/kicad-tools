#!/usr/bin/env bash
# scripts/ci/jobs/typecheck.sh -- baseline-gated mypy gate (Issue #3512).
# The single copy of the `typecheck` job's check command; both
# .github/workflows/ci.yml's `typecheck` job and scripts/ci/local-gate.sh run
# this file (2am#7561 ruling). The wrapper fails ONLY on errors beyond the
# committed baseline (.github/mypy-baseline.txt). Do NOT re-add
# continue-on-error or pad the baseline to silence a new error -- fix the
# type error.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Run mypy (baseline-gated)"
uv run python scripts/ci/check_mypy_baseline.py
