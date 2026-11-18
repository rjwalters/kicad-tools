#!/usr/bin/env bash
# scripts/ci/jobs/model-coverage-report.sh -- fleet 3D model coverage REPORT,
# never a gate (Issue #7682; `continue-on-error` in CI, advisory in the local
# gate). The single copy of the `model-coverage-report` job's check command;
# both .github/workflows/ci.yml's job and scripts/ci/local-gate.sh run this
# file (2am#7561 ruling). The pinned-model cache/sync step and the artifact
# upload are CI-only and stay as plain workflow steps.
#
# Usage: model-coverage-report.sh [OUTPUT-PATH]
#   default: a mktemp path, so a local run never leaves a file in the tree.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

out="${1:-$(mktemp -t model-coverage-XXXXXX).json}"

step "Write model coverage report"
uv run python scripts/ci/model_coverage_report.py -o "$out"
