"""Silk DRC rules agree between an origin-offset board and its (0,0) twin (Issue #8561).

``PCB.load`` shifts footprints (and so pads) into board-relative coordinates,
but board-level ``gr_text`` / ``gr_*`` keep the file's sheet-absolute values.
The silk rules must evaluate both in one frame.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_silk_edge_clearance,
    check_silk_over_copper,
    check_silk_overlap,
)

_TEMPLATE = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (44 "Edge.Cuts" user))
  (net 0 "")
  (gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "Resistor_SMD:R_0603" (layer "F.Cu") (at {rx} {ry})
    (property "Reference" "R1" (at 0 -1.5) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_line (start -1.5 1.2) (end 1.5 1.2) (stroke (width 0.12) (type solid))
      (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")))
  (gr_text "Widget B 2026-11-06" (at {tx} {ty}) (layer "F.SilkS")
    (effects (font (size 1 1) (thickness 0.15))))
  (gr_text "EDGE TEXT" (at {ex} {ey}) (layer "F.SilkS")
    (effects (font (size 1 1) (thickness 0.15))))
  (gr_text "BACK TEXT" (at {tx} {ty}) (layer "B.SilkS")
    (effects (font (size 1 1) (thickness 0.15)) (justify mirror)))
  (gr_line (start {lx0} {ly}) (end {lx1} {ly}) (stroke (width 0.15) (type solid))
    (layer "F.SilkS"))
)
"""


def _board(tmp_path: Path, ox: float, oy: float) -> Path:
    # Board-relative design: 40x30 board, R1 at (3,27), text on R1's pads,
    # a text crossing the left edge, and a gr_line over R1's fp_line.
    text = _TEMPLATE.format(
        x0=ox,
        y0=oy,
        x1=ox + 40,
        y1=oy + 30,
        rx=ox + 3,
        ry=oy + 27,
        tx=ox + 3,
        ty=oy + 27,
        ex=ox + 0.2,
        ey=oy + 10,
        lx0=ox + 1.5,
        lx1=ox + 4.5,
        ly=oy + 28.2,
    )
    path = tmp_path / f"board_{int(ox)}_{int(oy)}.kicad_pcb"
    path.write_text(text)
    return path


def _summary(pcb: PCB) -> dict[str, list[tuple]]:
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=2)
    out: dict[str, list[tuple]] = {}
    for fn in (check_silk_over_copper, check_silk_edge_clearance, check_silk_overlap):
        for v in fn(pcb, rules).violations:
            loc = (round(v.location[0], 3), round(v.location[1], 3)) if v.location else None
            out.setdefault(v.rule_id, []).append((v.layer, tuple(sorted(v.items)), loc))
    return {k: sorted(v, key=repr) for k, v in out.items()}


def test_origin_zero_twin_has_findings(tmp_path: Path):
    """Sanity: the fixture really trips each rule on the (0,0) board."""
    found = _summary(PCB.load(_board(tmp_path, 0, 0)))
    assert len(found.get("silk_over_copper", [])) >= 2
    assert found.get("silk_edge_clearance")
    assert found.get("silk_overlap")


@pytest.mark.parametrize("origin", [(100.0, 100.0), (50.5, 20.25)])
def test_offset_board_matches_origin_zero_twin(tmp_path: Path, origin: tuple[float, float]):
    twin = _summary(PCB.load(_board(tmp_path, 0, 0)))
    offset_pcb = PCB.load(_board(tmp_path, *origin))
    assert offset_pcb._board_origin == origin
    assert _summary(offset_pcb) == twin


def test_place_refs_static_silk_obstacles_match_origin_zero_twin(tmp_path: Path):
    """place_refs audit: board-level silk obstacles share the footprints' frame."""
    from kicad_tools.silkscreen.place_refs import _iter_static_silk_obstacles

    def bounds(pcb: PCB) -> list[tuple]:
        return sorted(
            (side, label, tuple(round(v, 3) for v in geom.bounds))
            for side, geom, label in _iter_static_silk_obstacles(pcb)
        )

    twin = bounds(PCB.load(_board(tmp_path, 0, 0)))
    assert twin
    assert bounds(PCB.load(_board(tmp_path, 100, 100))) == twin
