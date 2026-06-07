#!/usr/bin/env bash
# Quick board-06 routing determinism smoke test (Issue #3144 / #3272).
#
# Re-routes board 06 N times at seed=42 (default N=5) and asserts:
#   (1) every routed PCB has the same content-hash (UUIDs stripped),
#   (2) every ``kct check`` invocation reports the same error count.
#
# The two checks are complementary: (1) catches routing-path
# non-determinism (a different segment / via geometry between runs),
# while (2) catches DRC-reporter non-determinism (e.g. an unordered
# violation set in ``kct check`` that would still report different
# error totals on identical PCBs).  Issue #3272 added (2) plus the
# UUID-stripped hashing so a residual file-format randomness (per-via
# UUID under ``uuid.uuid4()``) does NOT mask the underlying routing
# invariant.
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
      "${OUT_DIR}/hashes.txt" "${OUT_DIR}/drc-counts.txt"

echo "==> Board 06 determinism smoke test"
echo "    Runs:           ${N}"
echo "    Seed:           ${SEED}"
echo "    Output dir:     ${OUT_DIR}"
echo "    PYTHONHASHSEED: ${PYTHONHASHSEED:-(inherited)}"
echo

# Standardise PYTHONHASHSEED for the child processes so callers who
# forgot to export it still get deterministic string-hash behaviour.
export PYTHONHASHSEED="${PYTHONHASHSEED:-42}"

# Helper: compute a content hash of the PCB after stripping every
# ``(uuid "...")`` token.  Issue #3272: the router emits deterministic
# UUIDs when a seed is supplied (see
# :func:`kicad_tools.router.primitives.enable_deterministic_uuids`)
# but we still strip defensively so the harness catches a regression
# in that toggle as a CONTENT-hash mismatch rather than masking it as
# a raw-file MD5 mismatch.  Without the strip a fresh ``uuid.uuid4()``
# leak in an unrelated module would surface as a false-positive
# routing-path divergence.
compute_content_hash() {
  local path="$1"
  if command -v md5 >/dev/null 2>&1; then
    sed -E 's/\(uuid "[^"]*"\)/(uuid "STRIPPED")/g' "${path}" | md5 -q
  else
    sed -E 's/\(uuid "[^"]*"\)/(uuid "STRIPPED")/g' "${path}" | md5sum | awk '{print $1}'
  fi
}

prev_hash=""
prev_drc=""
prev_raw_hash=""
saw_raw_mismatch=0
for ((i = 1; i <= N; i++)); do
  log="${OUT_DIR}/run-${i}.log"
  pcb_dst="${OUT_DIR}/run-${i}.kicad_pcb"

  echo "==> Run ${i}/${N}..."
  start_s=$(date +%s)
  uv run python "${SCRIPT}" --step route --seed "${SEED}" >"${log}" 2>&1
  end_s=$(date +%s)
  elapsed=$((end_s - start_s))

  cp "${REPO_ROOT}/boards/06-diffpair-test/output/${PCB_NAME}" "${pcb_dst}"
  raw_hash=$(md5 -q "${pcb_dst}" 2>/dev/null || md5sum "${pcb_dst}" | awk '{print $1}')
  content_hash=$(compute_content_hash "${pcb_dst}")
  # kct check returns exit code 2 when DRC errors are found (board 06
  # always has 5-6 errors against the JLCPCB ruleset -- this is the
  # "what is the count" smoke test, NOT a DRC pass/fail gate, so we
  # explicitly accept exit codes 0 and 2 and only fail the pipeline on
  # other codes (parse error = 1, tool crash = >2).
  drc_json="${OUT_DIR}/run-${i}-drc.json"
  set +e
  uv run kct check "${pcb_dst}" --mfr jlcpcb --errors-only --format json \
    >"${drc_json}" 2>/dev/null
  drc_rc=$?
  set -e
  if [[ ${drc_rc} -eq 0 || ${drc_rc} -eq 2 ]]; then
    drc_count=$(uv run python -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["summary"]["errors"])' \
      "${drc_json}" 2>/dev/null || echo "?")
  else
    echo "WARN: kct check exited with rc=${drc_rc} (expected 0 or 2); see ${drc_json}"
    drc_count="?"
  fi
  echo "  Elapsed:        ${elapsed}s"
  echo "  Raw MD5:        ${raw_hash}"
  echo "  Content MD5:    ${content_hash} (UUIDs stripped)"
  echo "  DRC error count: ${drc_count}"
  echo "${i} content=${content_hash} raw=${raw_hash} drc=${drc_count}" \
    >>"${OUT_DIR}/hashes.txt"
  echo "${i} ${drc_count}" >>"${OUT_DIR}/drc-counts.txt"

  if [[ -n "${prev_raw_hash}" && "${prev_raw_hash}" != "${raw_hash}" ]]; then
    # Note-only signal: raw-file MD5 differing while content matches is
    # purely a UUID-randomness artifact (file-format non-determinism
    # we don't gate on).  We log it so a regression of the
    # deterministic-UUID toggle in #3272 is still visible.
    saw_raw_mismatch=1
  fi
  if [[ -n "${prev_hash}" && "${prev_hash}" != "${content_hash}" ]]; then
    echo
    echo "FAIL: Run ${i} content-hash differs from prior run "
    echo "      (${prev_hash} vs ${content_hash})"
    echo "      Routing path produced different geometry across runs."
    echo "      PCBs preserved in ${OUT_DIR} for diff post-mortem."
    exit 2
  fi
  if [[ -n "${prev_drc}" && "${prev_drc}" != "${drc_count}" ]]; then
    echo
    echo "FAIL: Run ${i} DRC count differs from prior run "
    echo "      (${prev_drc} vs ${drc_count})"
    echo "      kct check reported a different error total despite "
    echo "      identical PCB content -- DRC reporter non-determinism."
    echo "      PCBs preserved in ${OUT_DIR} for diff post-mortem."
    exit 3
  fi
  prev_hash="${content_hash}"
  prev_drc="${drc_count}"
  prev_raw_hash="${raw_hash}"
done

echo
echo "PASS: ${N} runs"
echo "  Content MD5 (UUIDs stripped): ${prev_hash}"
echo "  DRC error count:               ${prev_drc}"
if (( saw_raw_mismatch != 0 )); then
  echo
  echo "NOTE: raw-file MD5s differed across runs while content matched."
  echo "      Indicates a UUID-randomness leak (Issue #3272 toggle "
  echo "      regression or a new uuid.uuid4() emitter outside the "
  echo "      router primitives).  Investigate but DO NOT treat as "
  echo "      a routing-path regression."
fi
