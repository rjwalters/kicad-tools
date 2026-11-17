#!/usr/bin/env bash
# Issue #8998 A/B benchmark: joint candidate selection OFF vs ON.
#
# Routes each fleet board twice with its production flags -- once with
# KCT_JOINT_CANDIDATE_SELECTION unset (OFF, the default path) and once
# with it =1 (ON) -- and extracts:
#   * overflow entering rip-up (the first "Routed X/Y nets, overflow: N"
#     line, printed right after Iteration 0),
#   * final overflow (the last such line),
#   * final routed/total nets and the completion verdict.
#
# Verdict and analysis: docs/research/kct-route-joint-candidate-selection-experiment.md
# (measured refutation -- the feature was NOT merged; this script is the
# measurement instrument the verdict rests on, kept for any future
# re-evaluation).
#
# Usage: scripts/research/joint_candidate_selection_experiment.sh <out-dir> [board...]
# Boards default to: 02 03 04 mains
#
# The selection feature is intentionally flag-less (Issue #8998
# out-of-scope): KCT_JOINT_CANDIDATE_SELECTION=1 is the env-based A/B
# benchmarking path documented in route_all_negotiated's docstring.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:?usage: scripts/research/joint_candidate_selection_experiment.sh <out-dir> [board...]}"
shift || true
if [[ $# -eq 0 ]]; then BOARDS=(02 03 04 mains); else BOARDS=("$@"); fi
export PYTHONHASHSEED=42

cd "$REPO_ROOT" || exit 1

route_board() {
    local board="$1" mode="$2" log="$3"
    local -a flags=()
    case "$board" in
      02)
        flags=(boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb
          --strategy negotiated --iterations 30 --deterministic-budget
          --timeout 900 --seed 42 --no-auto-pour --no-auto-layers
          --grid 0.1 --manufacturer jlcpcb --no-cache)
        ;;
      05)
        # design.py route_pcb() legacy recipe (documented plain-negotiated
        # BLDC board; --auto-layers 4==4 so no escalation ladder).
        flags=(boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb
          --auto-layers --starting-layers 4 --max-layers 4
          --manufacturer jlcpcb-tier1 --micro-via-in-pad-fallback
          --backend cpp --seed 7 --timeout 900 --per-net-timeout 60
          --allow-unsafe-grid --escape-corridor-reservation
          --skip-nets "+24V,+5V,+3V3,GND,PHASE_A,PHASE_B,PHASE_C"
          --no-reserve-plane-layers --no-cache)
        ;;
      03)
        # NOTE: board 03 auto-selects Two-Phase routing (Global + Detailed)
        # -- not the plain negotiated loop -- so the #8998 hook does not
        # apply to it (route_all_two_phase is out of scope for the issue).
        flags=(boards/03-usb-joystick/output/usb_joystick.kicad_pcb
          --seed 42 --manufacturer jlcpcb-tier1 --backend cpp
          --deterministic-budget --timeout 600 --no-cache
          --net-class-map boards/03-usb-joystick/output/net_class_map.json)
        ;;
      04)
        flags=(boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb
          --mfr jlcpcb-tier1 --auto-fix --no-auto-layers --layers 2
          --grid 0.05 --via-drill 0.15 --via-diameter 0.30
          --placement-feedback --no-cache --seed 42
          --deterministic-budget --timeout 600)
        ;;
      mains)
        local dir=boards/synthetic-mains-complete
        flags=("$dir/mains_fixture.kicad_pcb"
          --net-class-map "$dir/net_class_map.json"
          --voltage-map "$dir/voltage_map.json"
          --creepage-standard iec60664 --pollution-degree 2
          --material-group IIIa --hv-threshold 0 --skip-drc --no-auto-pour
          --nets "/LINE,/FUSED_LINE,/NEUT,/SENSE_D" --seed 42)
        ;;
      *) echo "unknown board $board" >&2; return 2;;
    esac
    if [[ "$mode" == "ON" ]]; then
        export KCT_JOINT_CANDIDATE_SELECTION=1
    else
        unset KCT_JOINT_CANDIDATE_SELECTION
    fi
    {
        echo "--- $board $mode start $(date -u +%H:%M:%S)"
        nice -n 10 uv run kct route "${flags[@]}" -o "$OUT_DIR/${board}-${mode}.kicad_pcb"
        echo "rc=$?"
    } >> "$log" 2>&1
}

mkdir -p "$OUT_DIR"
for board in "${BOARDS[@]}"; do
    for mode in OFF ON; do
        log="$OUT_DIR/${board}-${mode}.log"
        echo "=== board $board mode $mode"
        route_board "$board" "$mode" "$log"
        tail -1 "$log"
    done
done

echo
echo "=== summary"
printf "%-6s %-4s %-10s %-10s %-14s %s\n" board mode ripup final routed verdict
for board in "${BOARDS[@]}"; do
    for mode in OFF ON; do
        log="$OUT_DIR/${board}-${mode}.log"
        ripup=$(grep -o 'overflow: [0-9]*' "$log" | head -1 | awk '{print $2}')
        final=$(grep -o 'overflow: [0-9]*' "$log" | tail -1 | awk '{print $2}')
        routed=$(grep 'Routed .*/.* nets' "$log" | tail -1 |
            sed -n 's/.*Routed \([0-9]*\/[0-9]*\) nets.*/\1/p')
        verdict=$(grep -oE 'No conflicts - routing complete|routing complete|ROUTING [A-Z ]+' "$log" | tail -1)
        sel=$(grep -c 'Joint candidate selection:' "$log")
        printf "%-6s %-4s %-10s %-10s %-14s %s (sel-lines=%s)\n" \
            "$board" "$mode" "${ripup:-?}" "${final:-?}" "${routed:-?}" "${verdict:-?}" "$sel"
    done
done
