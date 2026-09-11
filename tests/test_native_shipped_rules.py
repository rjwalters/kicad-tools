"""Every distributed DRU must permit native KiCad to execute its rules."""

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli

RULES = Path(__file__).resolve().parents[1] / "src/kicad_tools/manufacturers/rules"
TEMPLATES = sorted(RULES.glob("*.kicad_dru"))


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_shipped_rules_execute_native_clearance_probe(tmp_path, template):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    board = tmp_path / "probe.kicad_pcb"
    board.write_text("""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
      (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default))
        (fill none) (layer "Edge.Cuts"))
      (footprint "Test:Pads" (layer "F.Cu") (at 10 10)
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "A"))
        (pad "2" smd rect (at 1.4 0) (size 1 1) (layers "F.Cu") (net 2 "B"))))""")
    board.with_suffix(".kicad_dru").write_text(
        template.read_text()
        + '\n(rule "Template parsing witness" (constraint clearance (min 0.9mm)))\n'
    )
    report = tmp_path / "native.json"
    subprocess.run(
        [str(cli), "pcb", "drc", str(board), "--format", "json", "-o", str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    violations = json.loads(report.read_text())["violations"]
    assert any(
        v["type"] == "clearance" and "Template parsing witness" in v["description"]
        for v in violations
    ), (template.name, violations)
