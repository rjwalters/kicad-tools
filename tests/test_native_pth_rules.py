"""Full emitted rule sets must remain executable in native KiCad DRC."""

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile, write_drc_constraints


@pytest.mark.parametrize("layers,expected_rings,x", [(2, 2, 10), (4, 0, 10), (2, 2, 0.65)])
def test_native_full_rules_enforce_pth_floor_without_hole_edge_false_positives(
    tmp_path: Path, layers: int, expected_rings: int, x: float
):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    inner = '(1 "In1.Cu" signal) (2 "In2.Cu" signal)' if layers == 4 else ""
    board = tmp_path / "probe.kicad_pcb"
    board.write_text(f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) {inner} (31 "B.Cu" signal)
        (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
      (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default))
        (fill none) (layer "Edge.Cuts"))
      (footprint "Test:PTH" (layer "F.Cu") (at {x} 10)
        (pad "1" thru_hole circle (at 0 0) (size 0.7 0.7) (drill 0.4)
          (layers "*.Cu" "*.Mask") (net 1 "A"))
        (pad "2" thru_hole circle (at 0.9 0) (size 0.7 0.7) (drill 0.4)
          (layers "*.Cu" "*.Mask") (net 2 "B"))))""")
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=layers, copper_oz=1)
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb-tier1", layers=layers)
    report = tmp_path / "native.json"
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(board)],
        check=True,
        capture_output=True,
        text=True,
    )
    violations = json.loads(report.read_text())["violations"]
    rings = [v for v in violations if v["type"] == "annular_width"]
    assert len(rings) == expected_rings, violations
    assert all("PTH Annular Ring" in v["description"] for v in rings)
    # The .35mm copper-to-adjacent-hole gap must not inherit a .4/.5mm
    # board-edge floor. Both pads are ten millimeters from the board edge.
    edge = [v for v in violations if "rule 'Hole to Edge" in v["description"]]
    assert bool(edge) == (x < 1), violations
    if x == 10:
        assert not [v for v in violations if v["type"] == "hole_clearance"], violations
