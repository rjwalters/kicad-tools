#!/usr/bin/env bash
# scripts/ci/jobs/kicad-cli-smoke.sh -- kicad-cli round-trip smoke gate.
# The single copy of the `kicad-cli-smoke` job's check commands; both
# .github/workflows/ci.yml's `kicad-cli-smoke` job and scripts/ci/local-gate.sh
# run this file (2am#7561 ruling). A regression gate that PCB files emitted by
# kicad-tools' writers are loadable by kicad-cli (two writer bugs shipped to
# main and broke every demo board's manufacturing export). SKIPs, never a
# silent PASS, when kicad-cli is absent (CI runs inside the pinned kicad
# container, so only the local gate can hit the skip).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

if ! command -v kicad-cli >/dev/null 2>&1; then
  die_skip "kicad-cli not found on PATH -- skipping (CI runs this in the pinned KiCad container)"
fi

step "kicad-cli version"
kicad-cli --version

step "Run kicad-cli round-trip smoke tests"
uv run pytest tests/test_kicad_cli_roundtrip.py -v --no-cov
