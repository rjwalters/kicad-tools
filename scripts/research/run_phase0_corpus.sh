#!/usr/bin/env bash
# Run the Phase 0 perturbation/labelling pipeline across the in-repo board corpus.
#
# Per-seed perturbation parameters are calibrated to land near a ~20-50%
# pass rate (the spec's expected 20/80 class balance).  Boards that route
# slowly use lower sigmas to keep the runtime tractable.
#
# All boards write to the same JSONL output so a single training run sees
# the full distribution.  Routing uses ``--backend cpp`` with a 45 s
# per-board cap, so worst-case wall-clock per sample is ~50 s.
#
# Usage:
#   bash scripts/research/run_phase0_corpus.sh
#
# Environment knobs:
#   SAMPLES=N    samples per fast-routing seed (default: 40)
#   SEED=N       master RNG seed (default: 42)
#   OUTPUT=PATH  labels jsonl (default: data/research/fom_phase0/labels.jsonl)
set -euo pipefail

SAMPLES="${SAMPLES:-40}"
SEED="${SEED:-42}"
OUTPUT="${OUTPUT:-data/research/fom_phase0/labels.jsonl}"
WORK_DIR="${WORK_DIR:-data/research/fom_phase0/work}"

PY="uv run python scripts/research/generate_perturbations.py"
COMMON="--output ${OUTPUT} --work-dir ${WORK_DIR} --seed ${SEED} --cleanup-perturbed --route-timeout 45"

# Fast-routing boards get aggressive perturbations -> roughly 20-50% pass.
${PY} ${COMMON} --boards boards/01-voltage-divider/output/voltage_divider.kicad_pcb \
    --samples-per-seed ${SAMPLES} --sigma 4.0 --rotate-prob 0.3
${PY} ${COMMON} --boards boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \
    --samples-per-seed ${SAMPLES} --sigma 2.0 --rotate-prob 0.15

# Medium boards: gentler perturbations.
HALF=$(( SAMPLES * 3 / 4 ))
${PY} ${COMMON} --boards boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
    --samples-per-seed ${HALF} --sigma 1.5 --rotate-prob 0.1
${PY} ${COMMON} --boards boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb \
    --samples-per-seed ${HALF} --sigma 1.5 --rotate-prob 0.1
${PY} ${COMMON} --boards boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb \
    --samples-per-seed ${HALF} --sigma 1.5 --rotate-prob 0.1

# Hardest boards: very gentle, fewer samples (avoid runaway).
FEW=$(( SAMPLES / 2 ))
${PY} ${COMMON} --boards boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb \
    --samples-per-seed ${FEW} --sigma 1.0 --rotate-prob 0.05
${PY} ${COMMON} --boards boards/06-diffpair-test/output/diffpair_test.kicad_pcb \
    --samples-per-seed ${FEW} --sigma 1.0 --rotate-prob 0.05

echo "All seeds processed. Output: ${OUTPUT}"
