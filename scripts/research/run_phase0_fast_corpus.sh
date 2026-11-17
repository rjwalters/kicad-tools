#!/usr/bin/env bash
# Faster Phase 0 corpus runner: shorter timeouts, broader seed coverage.
#
# Used after a long initial run to add more seed diversity within a bounded
# wall-clock budget.  Per-board sigma is tuned so a 30 s router timeout
# captures the "completes in reasonable time AND clean DRC" notion.  Boards
# whose original layout doesn't route in 30 s seed almost entirely-negative
# labels, which is fine for the classifier -- it just means those rows
# encode "this seed + this perturbation -> fail."
set -euo pipefail

SEED="${SEED:-42}"
OUTPUT="${OUTPUT:-data/research/fom_phase0/labels.jsonl}"
WORK_DIR="${WORK_DIR:-data/research/fom_phase0/work}"

PY="uv run python scripts/research/generate_perturbations.py"
COMMON="--output ${OUTPUT} --work-dir ${WORK_DIR} --seed ${SEED} --cleanup-perturbed --route-timeout 30"

# USB joystick: gentle perturbation, fewer samples to keep wall-clock down.
${PY} ${COMMON} --boards boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
    --samples-per-seed 25 --sigma 1.0 --rotate-prob 0.05
${PY} ${COMMON} --boards boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb \
    --samples-per-seed 25 --sigma 1.0 --rotate-prob 0.05
${PY} ${COMMON} --boards boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb \
    --samples-per-seed 25 --sigma 1.0 --rotate-prob 0.05

echo "Fast corpus pass complete. Output: ${OUTPUT}"
