#!/usr/bin/env bash
# Native-execution helper for the `routed-pcb-drc-check` CI job (issue #5349).
#
# scripts/ci/check_routed_drc.py dispatches Board04's committed routed PCB
# through the reviewed paid mechanical-drill validator
# (boards/04-stm32-devboard/check_manufacturing.py), which calls strict
# `run_meta_checks(..., strict=True)` -- including native ERC via
# `kicad-cli`. The `routed-pcb-drc-check` job otherwise stays on bare
# ubuntu-latest (no KiCad) so PRs that don't touch a native-requiring board
# keep the cheap no-files short-circuit. This script is invoked via
# `docker run kicad/kicad:10.0 ...` from a single conditional step -- ONLY
# when `scripts/ci/select_routed_pcbs.py` determined a native-requiring
# board was genuinely selected -- rather than making the whole job pay the
# container-pull cost unconditionally.
#
# This intentionally reuses the SAME `kicad/kicad:10.0` image and
# stock-library-init step already used by the `test`, `kicad-cli-smoke`,
# `diffpair-routing-regression`, and `matchgroup-routing-regression` jobs
# elsewhere in ci.yml, rather than installing KiCad via apt/PPA (see those
# jobs' comments for why: unreliable Launchpad mirrors silently fall back to
# a KiCad-7-era package).
#
# Usage: run_native_routed_drc.sh <routed_pcb_path> [<routed_pcb_path> ...]
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "run_native_routed_drc.sh: no files given -- nothing to do" >&2
  exit 0
fi

echo "Ensuring container has prerequisites..."
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git

# Attributable native-capability probe: fail THIS step immediately and
# clearly if kicad-cli is missing/broken, instead of letting the paid-drill
# validator surface it later as a generic "ERC did not run" INCOMPLETE
# result deep inside check_manufacturing.py.
kicad-cli --version

python3 scripts/ci/init_kicad_libraries.py

echo "Installing uv..."
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# The repo checkout is bind-mounted from the host runner (see the calling
# workflow step's `docker run -v "$PWD:$PWD"`). If the host-side job also
# ran `uv sync` for the ordinary-board path (both native_files AND
# ordinary_files selected in the same PR), a repo-local `.venv/` may already
# exist there, created by (and symlinked into) the HOST's uv Python
# toolchain cache -- a path that does not exist inside this container. Point
# this container's venv at an isolated, container-local directory so it
# never reads or clobbers that host-side `.venv/`.
export UV_PROJECT_ENVIRONMENT=/opt/kct-native-venv

uv sync --frozen --extra dev --python 3.12

echo "Running native routed-PCB DRC check on: $*"
uv run python scripts/ci/check_routed_drc.py "$@"
