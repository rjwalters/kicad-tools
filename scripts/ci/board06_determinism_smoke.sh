#!/usr/bin/env bash
# Quick board-06 routing determinism smoke test (Issue #3144).
#
# Re-routes board 06 N times at seed=42 (default N=5) and asserts the
# resulting PCBs have identical MD5 hashes.  Used by the CI determinism
# regression job and as a hand-runnable diagnostic when investigating
# suspected new non-determinism vectors.
#
# Usage:
#   ./scripts/ci/board06_determinism_smoke.sh        # 5 runs
#   ./scripts/ci/board06_determinism_smoke.sh 3      # 3 runs (faster)
#
# Each run takes ~6-9 min wall-clock on local 8-core hardware and
# ~20-30 min on a 2-core CI runner.  The script bails on the first
# divergence rather than running the full N x ~9min loop.

set -euo pipefail

N="${1:-5}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${BOARD06_DETERMINISM_OUT:-/tmp/board06-determinism}"
SEED="${BOARD06_DETERMINISM_SEED:-42}"
SCRIPT="${REPO_ROOT}/boards/06-diffpair-test/generate_design.py"
PCB_NAME="diffpair_test_routed.kicad_pcb"

if [[ ! -f "${SCRIPT}" ]]; then
  echo "ERROR: generate_design.py not found at ${SCRIPT}" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"
rm -f "${OUT_DIR}"/run-*.log "${OUT_DIR}"/run-*.kicad_pcb \
      "${OUT_DIR}/hashes.txt"

echo "==> Board 06 determinism smoke test"
echo "    Runs:           ${N}"
echo "    Seed:           ${SEED}"
echo "    Output dir:     ${OUT_DIR}"
echo "    PYTHONHASHSEED: ${PYTHONHASHSEED:-(inherited)}"
echo

# Standardise PYTHONHASHSEED for the child processes so callers who
# forgot to export it still get deterministic string-hash behaviour.
export PYTHONHASHSEED="${PYTHONHASHSEED:-42}"

prev_hash=""
for ((i = 1; i <= N; i++)); do
  log="${OUT_DIR}/run-${i}.log"
  pcb_dst="${OUT_DIR}/run-${i}.kicad_pcb"

  echo "==> Run ${i}/${N}..."
  start_s=$(date +%s)
  uv run python "${SCRIPT}" --step route --seed "${SEED}" >"${log}" 2>&1
  end_s=$(date +%s)
  elapsed=$((end_s - start_s))

  cp "${REPO_ROOT}/boards/06-diffpair-test/output/${PCB_NAME}" "${pcb_dst}"
  hash=$(md5 -q "${pcb_dst}" 2>/dev/null || md5sum "${pcb_dst}" | awk '{print $1}')
  echo "  Elapsed: ${elapsed}s"
  echo "  MD5:     ${hash}"
  echo "${i} ${hash}" >>"${OUT_DIR}/hashes.txt"

  if [[ -n "${prev_hash}" && "${prev_hash}" != "${hash}" ]]; then
    echo
    echo "FAIL: Run ${i} hash differs from prior run (${prev_hash} vs ${hash})"
    echo "      PCBs preserved in ${OUT_DIR} for diff post-mortem."
    exit 2
  fi
  prev_hash="${hash}"
done

echo
echo "PASS: All ${N} runs produced identical PCB output (MD5 ${prev_hash})."
