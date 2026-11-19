#!/usr/bin/env bash
# scripts/ci/jobs/test-extended.sh -- the ci_extended slow tail (Issue #6013).
# The single copy of the `test-extended` job's check command; both
# .github/workflows/ci.yml's `test-extended` job and scripts/ci/local-gate.sh
# run this file (2am#7561 ruling). Runs exactly the tests tests/ci_extended.txt
# marks, which `test` deselects. CI runs it on main pushes and the nightly
# schedule only; locally it is a long job. Extra arguments are forwarded to
# pytest (CI adds --junitxml for the test-timing artifact that
# scripts/ci/update_ci_extended.py consumes).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Build C++ router backend"
uv run kct build-native

step "Run ci_extended tests"
uv run pytest -n auto -rfEsx -o addopts= --benchmark-disable --timeout=60 \
  -m "ci_extended and not slow" "$@"
