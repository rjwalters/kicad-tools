#!/usr/bin/env bash
# scripts/ci/jobs/release-changelog-gap.sh -- release extra (NOT a ci.yml
# job): the changelog gap report. Part of the Actions-outage release gate
# (`local-gate.sh --release`); see RELEASING.md "Actions outage fallback".
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Changelog gap report"
uv run python scripts/changelog_gap_report.py
