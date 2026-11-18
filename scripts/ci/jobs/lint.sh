#!/usr/bin/env bash
# scripts/ci/jobs/lint.sh -- Lint & Format checks.
# The single copy of the `lint` job's gating commands; both
# .github/workflows/ci.yml's `lint` job and scripts/ci/local-gate.sh run this
# file (2am#7561 ruling). The job is deliberately unconditional in ci.yml
# (no `needs`/`if`) so the content contracts run on every event, including an
# hourly run that skips everything else (#5986) -- see
# tests/test_content_contract_gate.py. The changes-filter verification
# (verify_changes_filter.mjs + its ephemeral npm install) is CI-only and
# stays as plain workflow steps.
#
# If red, run `uv run ruff format .` and `uv run ruff check . --fix` -- do
# NOT make these steps advisory (issue #3464).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Check formatting"
uv run ruff format . --check

step "Lint"
uv run ruff check .

step "Shellcheck the CI gate scripts"
command -v shellcheck >/dev/null 2>&1 || die_skip "shellcheck not found; skipping (CI's ubuntu-latest image ships it)"
shellcheck -s bash -S warning scripts/ci/jobs/*.sh scripts/ci/local-gate.sh

step "Check documentation and agent-content contracts"
bash scripts/ci/check-content-contracts.sh
