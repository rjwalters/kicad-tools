"""Board 03 footprint silk clears the board edge (Issue #6494).

J1 (GCT USB4085 USB-C) overhangs the top edge by design; two of its body
outline ``fp_line``s used to end 0.125 mm (ink) from Edge.Cuts, under the
0.20 mm ``silk_edge_clearance`` floor.  ``silk_repair.trim_silk_to_edge_clearance``
clips them at placement time; these tests pin the committed artifacts and the
clip geometry.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.silkscreen._silk_defaults import SILK_EDGE_CLEARANCE_MM  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_silk_edge_clearance  # noqa: E402

BOARD = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
SILK_LAYERS = {"F.SilkS", "B.SilkS"}


def _silk_repair():
    spec = importlib.util.spec_from_file_location(
        "board03_silk_repair_6494", BOARD / "silk_repair.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _edge_findings(path: Path) -> list[str]:
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=4)
    return [v.message for v in check_silk_edge_clearance(PCB.load(path), rules).violations]


@pytest.mark.parametrize("name", ["usb_joystick.kicad_pcb", "usb_joystick_routed.kicad_pcb"])
def test_board03_artifacts_have_no_silk_edge_clearance_finding(name: str):
    assert _edge_findings(BOARD / "output" / name) == []


@pytest.mark.parametrize("name", ["usb_joystick.kicad_pcb", "usb_joystick_routed.kicad_pcb"])
def test_j1_outline_is_trimmed_not_deleted(name: str):
    """All six J1 silk lines survive; the two edge-side verticals are shortened."""
    j1 = next(fp for fp in PCB.load(BOARD / "output" / name).footprints if fp.reference == "J1")
    lines = sorted(
        (g.start, g.end) for g in j1.graphics if g.layer in SILK_LAYERS and g.graphic_type == "line"
    )
    assert len(lines) == 6
    verticals = [
        (s, e) for s, e in lines if s[0] == e[0] and s[0] in (-1.62, 7.57) and e[1] == 8.73
    ]
    assert len(verticals) == 2
    for start, _end in verticals:
        assert start[1] > 6.0  # clipped back from the stock 6.0 mm


def test_floor_comes_from_the_rule_constant():
    assert _silk_repair().silk_edge_floor() == SILK_EDGE_CLEARANCE_MM


_SYNTHETIC = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "00000000-0000-4000-8000-000000000001"))
  (footprint "Test:Probe" (layer "F.Cu") (uuid "00000000-0000-4000-8000-000000000002")
    (at 10 10)
    (property "Reference" "U1" (at 0 0) (layer "F.SilkS") (uuid "00000000-0000-4000-8000-000000000003")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_line (start -2 -9.95) (end 2 -9.95) (stroke (width 0.15) (type default))
      (layer "F.SilkS") (uuid "00000000-0000-4000-8000-000000000004"))
    (fp_line (start -12 -5) (end -5 -5) (stroke (width 0.15) (type default))
      (layer "F.SilkS") (uuid "00000000-0000-4000-8000-000000000005"))
    (fp_line (start 2 2) (end 4 4) (stroke (width 0.15) (type default))
      (layer "F.SilkS") (uuid "00000000-0000-4000-8000-000000000006"))
  )
)
"""


def test_synthetic_edge_cases(tmp_path: Path):
    """Band-hugging line dropped; edge-crossing line clipped; clear line untouched."""
    pcb_path = tmp_path / "probe.kicad_pcb"
    pcb_path.write_text(_SYNTHETIC)
    assert len(_edge_findings(pcb_path)) == 2

    result = _silk_repair().trim_silk_to_edge_clearance(pcb_path)

    assert result.residual == []
    assert result.lines_removed == 1  # runs along y=0.05, wholly inside the band
    assert result.lines_trimmed == 1  # crosses x=0 -> both clear ends kept
    assert result.lines_added == 1
    assert _edge_findings(pcb_path) == []

    u1 = next(fp for fp in PCB.load(pcb_path).footprints if fp.reference == "U1")
    lines = sorted((g.start, g.end) for g in u1.graphics if g.graphic_type == "line")
    assert ((2.0, 2.0), (4.0, 4.0)) in lines  # untouched
    crossing = [(s, e) for s, e in lines if s[1] == -5.0]
    assert len(crossing) == 2
    floor = SILK_EDGE_CLEARANCE_MM + 0.15 / 2
    board_x = sorted(10 + x for s, e in crossing for x in (s[0], e[0]))
    # Outside piece ends >= floor left of x=0; inside piece starts >= floor right.
    assert board_x[1] <= -floor
    assert board_x[2] >= floor
