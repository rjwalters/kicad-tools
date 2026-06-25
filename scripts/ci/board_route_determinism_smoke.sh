#!/usr/bin/env bash
# Routed-copper determinism smoke for a board's UNROUTED PCB (Issue #3799).
#
# Routes a board's committed unrouted ``output/<stem>.kicad_pcb`` N times
# (default N=2) with the board's production ``kct route`` flags -- which
# now include ``--deterministic-budget`` + ``--seed 42`` + a pinned
# ``PYTHONHASHSEED=42`` -- and asserts that the UUID-normalized routed
# COPPER (the ``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` set) is
# byte-identical across every run.
#
# WHY this exists (Issue #3799 root cause):
#   ``--seed`` only seeds Python's global ``random``.  It does NOT control
#   the per-net A* WALL-CLOCK cutoff (``--per-net-timeout``, default 30 s)
#   checked inside the C++ A* loop.  On a loaded machine that budget fires
#   mid-search and the net lands less copper -- SAME seed, DIFFERENT copper.
#   ``--deterministic-budget`` (#3538) swaps the wall-clock cutoff for a
#   fixed node-expansion ITERATION budget, so the abort point is
#   machine-independent and the seed-42 route is reproducible.  This smoke
#   is the regression gate proving boards 02/03/04 stay reproducible.
#
# Usage:
#   ./scripts/ci/board_route_determinism_smoke.sh <board-number> [runs]
#
# Examples:
#   ./scripts/ci/board_route_determinism_smoke.sh 02        # 2 runs
#   ./scripts/ci/board_route_determinism_smoke.sh 04 3      # 3 runs
#   ./scripts/ci/board_route_determinism_smoke.sh 05        # 2 runs
#
# Supported boards: 02 (charlieplex-led), 03 (usb-joystick),
# 04 (stm32-devboard), 05 (bldc-motor-controller).  Each board's flag set
# mirrors the ``kct route`` argv in its
# ``boards/<dir>/{generate_,}design.py:route_pcb()``.  KEEP THE FLAG LISTS
# BELOW IN SYNC with the recipes.
#
# NOTE (issue #3887): board 05 IS now covered.  Its main pass was migrated from
# the load-sensitive wall-clock ``--per-net-timeout 60`` cutoff to a fixed
# per-net ITERATION budget (``--deterministic-budget --per-net-iterations
# 200000``), so a route-twice-identical-copper check is now meaningful and the
# dense BLDC re-route terminates within the CI job limit.  This re-adds the
# board-05 case that #3880/PR #3886 had deferred.

set -euo pipefail

BOARD="${1:-}"
N="${2:-2}"

if [[ -z "${BOARD}" ]]; then
  echo "ERROR: board number required (02, 03, 04, or 05)" >&2
  echo "Usage: $0 <board-number> [runs]" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Per-board configuration: directory, unrouted-PCB stem, and the exact
# ``kct route`` flag list from generate_design.py:route_pcb().  These flag
# lists are the load-bearing part of the test -- they MUST mirror the
# recipe so the smoke routes the SAME copper the production recipe emits.
case "${BOARD}" in
  02)
    BOARD_DIR="boards/02-charlieplex-led"
    STEM="charlieplex_3x3"
    ROUTE_FLAGS=(
      --strategy negotiated
      --iterations 30
      --deterministic-budget
      --timeout 240
      --seed 42
      --skip-nets GND
      --manufacturer jlcpcb
    )
    ;;
  03)
    BOARD_DIR="boards/03-usb-joystick"
    STEM="usb_joystick"
    ROUTE_FLAGS=(
      --seed 42
      --manufacturer jlcpcb-tier1
      --backend cpp
      --deterministic-budget
      --timeout 600
    )
    ;;
  04)
    BOARD_DIR="boards/04-stm32-devboard"
    STEM="stm32_devboard"
    ROUTE_FLAGS=(
      --mfr jlcpcb-tier1
      --auto-fix
      --auto-layers
      --auto-mfr-tier
      --placement-feedback
      --micro-via-in-pad-fallback
      --seed 42
      --deterministic-budget
      --timeout 600
    )
    ;;
  05)
    # Issue #3887: mirrors boards/05-bldc-motor-controller/design.py:route_pcb()
    # (note: board 05's recipe file is design.py, not generate_design.py).  The
    # board-05-tuned --per-net-iterations 200000 is the deterministic per-net
    # cap (_BOARD_05_PER_NET_ITERATIONS) that keeps the dense BLDC re-route both
    # reproducible AND terminating within the 90-min CI job limit; KEEP IT IN
    # SYNC with the recipe.  Seed 7 (not 42) per the recipe's measured best.
    BOARD_DIR="boards/05-bldc-motor-controller"
    STEM="bldc_controller"
    ROUTE_FLAGS=(
      --auto-layers
      --starting-layers 4
      --max-layers 4
      --manufacturer jlcpcb-tier1
      --micro-via-in-pad-fallback
      --backend cpp
      --seed 7
      --deterministic-budget
      --per-net-iterations 200000
      --timeout 900
      --skip-nets "+24V,+5V,+3V3,GND,PHASE_A,PHASE_B,PHASE_C"
    )
    ;;
  *)
    echo "ERROR: unsupported board '${BOARD}' (supported: 02, 03, 04, 05)" >&2
    exit 1
    ;;
esac

INPUT="${REPO_ROOT}/${BOARD_DIR}/output/${STEM}.kicad_pcb"
OUT_DIR="${BOARD_ROUTE_DETERMINISM_OUT:-/tmp/board-route-determinism-${BOARD}}"

if [[ ! -f "${INPUT}" ]]; then
  echo "ERROR: unrouted PCB not found at ${INPUT}" >&2
  echo "       Run the board recipe once to generate it." >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"
rm -f "${OUT_DIR}"/run-*.kicad_pcb "${OUT_DIR}"/run-*.norm "${OUT_DIR}"/run-*.log

# Pin PYTHONHASHSEED for the child route processes -- mirrors the recipe's
# own pinning (#3799) so dict/set string-iteration entropy can never
# re-enter, matching board-07's convention.
export PYTHONHASHSEED="${PYTHONHASHSEED:-42}"

echo "==> Board ${BOARD} routed-copper determinism smoke (Issue #3799)"
echo "    Input:          ${INPUT}"
echo "    Runs:           ${N}"
echo "    Flags:          ${ROUTE_FLAGS[*]}"
echo "    PYTHONHASHSEED: ${PYTHONHASHSEED}"
echo "    Output dir:     ${OUT_DIR}"
echo

# Normalize routed copper: keep only (segment|via|arc) lines, strip the
# per-element UUID / tstamp tokens (which are deterministic per-seed but
# stripped defensively), and sort so element ORDER in the file does not
# matter -- only the SET of copper geometry.
normalize_copper() {
  sed -E 's/\(uuid "[^"]*"\)/(uuid "X")/g; s/\(tstamp [^)]*\)/(tstamp X)/g' "$1" \
    | grep -E '^[[:space:]]*\((segment|via|arc)' \
    | sort
}

prev_norm=""
for ((i = 1; i <= N; i++)); do
  pcb="${OUT_DIR}/run-${i}.kicad_pcb"
  log="${OUT_DIR}/run-${i}.log"
  norm="${OUT_DIR}/run-${i}.norm"

  echo "==> Run ${i}/${N}..."
  start_s=$(date +%s)
  # ``kct route`` exits non-zero on partial routing (codes 2/3); the routed
  # PCB is still written, so we tolerate a non-zero exit and let the copper
  # comparison be the gate.  Fatal codes (1 crash / 5 SIGINT) surface as an
  # empty / missing output, which the existence check below catches.
  PYTHONHASHSEED="${PYTHONHASHSEED}" uv run kct route "${INPUT}" \
    --output "${pcb}" "${ROUTE_FLAGS[@]}" >"${log}" 2>&1 || true
  end_s=$(date +%s)

  if [[ ! -s "${pcb}" ]]; then
    echo "FAIL: run ${i} produced no routed PCB (see ${log})" >&2
    tail -20 "${log}" >&2 || true
    exit 2
  fi

  normalize_copper "${pcb}" >"${norm}"
  echo "  Elapsed:     $((end_s - start_s))s"
  echo "  Copper lines: $(wc -l <"${norm}")"

  if [[ -n "${prev_norm}" ]]; then
    if ! diff -q "${prev_norm}" "${norm}" >/dev/null; then
      echo
      echo "FAIL: run ${i} routed copper differs from the prior run."
      echo "      Same board + --seed 42 + --deterministic-budget +"
      echo "      PYTHONHASHSEED=42 produced DIFFERENT copper -- the"
      echo "      determinism guarantee regressed (Issue #3799)."
      echo "      Normalized copper diff (first 40 lines):"
      diff "${prev_norm}" "${norm}" | head -40
      echo "      PCBs preserved in ${OUT_DIR} for post-mortem."
      exit 3
    fi
  fi
  prev_norm="${norm}"
done

echo
echo "PASS: board ${BOARD} routed byte-identical copper across ${N} runs."
