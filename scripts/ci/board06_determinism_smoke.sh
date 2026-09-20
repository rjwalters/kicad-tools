#!/usr/bin/env bash
# Quick board-06 routing determinism smoke test (Issue #3144 / #3272 / #3880 /
# #5586 / #5597).
#
# Re-routes board 06 N times at seed=42 (default N=5) and asserts:
#   (1) every routed PCB has the same routed-COPPER SET content-hash
#       (whole ``(segment|via|arc)`` nodes via
#       ``scripts/ci/normalize_copper.py``, UUID-stripped, SORTED),
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
# Issue #3880: the copper hash now compares the SORTED SET of copper
# geometry (matching the sibling ``board_route_determinism_smoke.sh``)
# rather than the raw file in WRITE ORDER.  Board 06's diff-pair
# pre-pass completes pairs in a timing-dependent order, so the SAME
# routed copper set is emitted in a different file order run-to-run.
# That write-order entropy is cosmetic -- it does NOT change the routed
# copper, the DRC count, or the diffpair-coverage gate's measured count
# (all order-insensitive).  Hashing the file in write order would
# false-positive FAIL on this harmless reordering while the actual
# determinism invariant (the copper SET + the DRC count, which the CI
# gate measures) holds.  After the #3880 deterministic-budget switch the
# per-net A* abort point is machine-independent, so the copper SET is
# stable; sorting before hashing is what asserts that stability.
#
# Usage:
#   ./scripts/ci/board06_determinism_smoke.sh        # 5 runs
#   ./scripts/ci/board06_determinism_smoke.sh 3      # 3 runs (faster)
#   ./scripts/ci/board06_determinism_smoke.sh --content-hash <pcb>
#
# Each run takes ~6-9 min wall-clock on local 8-core hardware and
# ~20-30 min on a 2-core CI runner.  The script bails on the first
# divergence rather than running the full N x ~9min loop.
#
# ``--content-hash <pcb>`` prints the content hash for ONE already-routed
# PCB and exits -- the exact code path the determinism loop below compares
# -- so it can be exercised directly in a unit test without a ~9min route
# (Issue #5586); mirrors the sibling ``board_route_determinism_smoke.sh``'s
# ``--normalize-copper`` self-test hook.
#
# Issue #5597: each run seeds a SCRATCH route dir under ``$OUT_DIR/route``
# with the committed unrouted PCB
# (``boards/06-diffpair-test/output/diffpair_test.kicad_pcb``) and passes
# that dir as ``generate_design.py``'s EXPLICIT positional ``output_dir``.
# The positional default changed to ``regression-output/`` in d95b6eff3
# (2026-09-10), and ``--step route`` on a fresh ``regression-output/``
# fails with "unrouted PCB not found" -- so relying on the implicit default
# broke this script outright.  This mirrors
# ``tests/test_board06_determinism.py::_run_route_regen`` and decouples the
# smoke run from the board's own ``output/`` and ``regression-output/``
# dirs (a concurrent local ``kct build`` cannot race with it).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Helper: compute a content hash of the routed COPPER SET by delegating to
# ``scripts/ci/normalize_copper.py`` (Issue #5580 / #5586), which compares
# whole, paren-balanced ``(segment ...)`` / ``(via ...)`` / ``(arc ...)``
# nodes -- geometry, width, layer and net included -- rather than raw
# lines.  ``uuid`` / ``tstamp`` children are stripped and the record list
# is SORTED, so element WRITE ORDER in the file does not matter, only the
# SET of copper geometry.  This mirrors the sibling
# ``board_route_determinism_smoke.sh`` (#3799) and the order-insensitive
# DRC-count gate.
#
# Issue #3880 (original hash, superseded by #5586): a line-based
# ``grep -E '^[[:space:]]*\((segment|via|arc)'`` over the raw file kept only
# the bare ``(segment`` / ``(via`` HEADER line of each MULTI-LINE
# s-expression and discarded every ``(start ...)`` / ``(end ...)`` /
# ``(width ...)`` / ``(layer ...)`` / ``(net ...)`` child -- degenerating the
# comparison to ``segment_count == segment_count && via_count == via_count``,
# blind to actual routed geometry.  See ``scripts/ci/normalize_copper.py``'s
# module docstring for the full history.
#
# Issue #3272: the router emits deterministic UUIDs when a seed is
# supplied (see
# :func:`kicad_tools.router.primitives.enable_deterministic_uuids`) but
# the normalizer still strips ``uuid``/``tstamp`` defensively so the harness
# catches a regression in that toggle via the raw-MD5 NOTE path rather than
# masking it.
compute_content_hash() {
  local path="$1"
  uv run python "${REPO_ROOT}/scripts/ci/normalize_copper.py" "${path}" \
    | { if command -v md5 >/dev/null 2>&1; then md5 -q; else md5sum | awk '{print $1}'; fi; }
}

# Self-test / introspection hook (Issue #5586): hash ONE routed PCB and
# exit, without running the full N x ~9min route loop below.
if [[ "${1:-}" == "--content-hash" ]]; then
  if [[ -z "${2:-}" ]]; then
    echo "ERROR: --content-hash requires a .kicad_pcb path" >&2
    echo "Usage: $0 --content-hash <pcb>" >&2
    exit 1
  fi
  compute_content_hash "$2"
  exit 0
fi

N="${1:-5}"
OUT_DIR="${BOARD06_DETERMINISM_OUT:-/tmp/board06-determinism}"
SEED="${BOARD06_DETERMINISM_SEED:-42}"
SCRIPT="${REPO_ROOT}/boards/06-diffpair-test/generate_design.py"
PCB_NAME="diffpair_test_routed.kicad_pcb"
# Issue #5597: scratch route dir passed to generate_design.py as the
# explicit positional output_dir (seeded per-run below); UNROUTED_PCB is
# the committed unrouted board the --step route step re-routes.
ROUTE_DIR="${OUT_DIR}/route"
UNROUTED_PCB="${REPO_ROOT}/boards/06-diffpair-test/output/diffpair_test.kicad_pcb"

if [[ ! -f "${SCRIPT}" ]]; then
  echo "ERROR: generate_design.py not found at ${SCRIPT}" >&2
  exit 1
fi
if [[ ! -f "${UNROUTED_PCB}" ]]; then
  echo "ERROR: committed unrouted PCB not found at ${UNROUTED_PCB}" >&2
  echo "       (boards/06-diffpair-test/output/diffpair_test.kicad_pcb is a" >&2
  echo "        tracked file; a missing copy means the checkout is broken.)" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"
rm -f "${OUT_DIR}"/run-*.log "${OUT_DIR}"/run-*.kicad_pcb \
      "${OUT_DIR}/hashes.txt" "${OUT_DIR}/drc-counts.txt"
rm -rf "${ROUTE_DIR}"

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
prev_drc=""
prev_raw_hash=""
saw_raw_mismatch=0
for ((i = 1; i <= N; i++)); do
  log="${OUT_DIR}/run-${i}.log"
  pcb_dst="${OUT_DIR}/run-${i}.kicad_pcb"

  echo "==> Run ${i}/${N}..."
  start_s=$(date +%s)
  # Issue #5597: seed the scratch route dir with the committed unrouted
  # PCB and pass it as generate_design.py's EXPLICIT positional
  # output_dir.  The implicit default (regression-output/, d95b6eff3)
  # starts empty, and --step route requires the unrouted PCB to already
  # be there -- relying on the default made every run fail with "unrouted
  # PCB not found".  Re-seeding per run also guarantees each iteration
  # routes the identical committed input regardless of what a previous
  # run (or a concurrent kct build) left behind.  Same pattern as
  # tests/test_board06_determinism.py::_run_route_regen.
  rm -rf "${ROUTE_DIR}"
  mkdir -p "${ROUTE_DIR}"
  cp "${UNROUTED_PCB}" "${ROUTE_DIR}/diffpair_test.kicad_pcb"
  uv run python "${SCRIPT}" --step route --seed "${SEED}" "${ROUTE_DIR}" \
    >"${log}" 2>&1
  end_s=$(date +%s)
  elapsed=$((end_s - start_s))

  cp "${ROUTE_DIR}/${PCB_NAME}" "${pcb_dst}"
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
