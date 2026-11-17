"""NPTH obstacles and own-courtyard association in place-silk-refs (#8628)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.silkscreen.place_refs import ASSOCIATION_MARGIN_MM, SilkRefPlacer

pytest.importorskip("shapely")

HEADER = """(kicad_pcb
  (version 20240108) (generator "test") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_line (start 0 0) (end 60 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 60 0) (end 60 40) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 60 40) (end 0 40) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 0 40) (end 0 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
"""


def _fp(ref, pos, ref_local, pads="", half=(0.5, 0.3)):
    hx, hy = half
    court = (
        f"(fp_rect (start {-hx} {-hy}) (end {hx} {hy}) (stroke (width 0.05) (type solid)) "
        '(layer "F.CrtYd") (fill none))'
    )
    return (
        f'(footprint "FP" (layer "F.Cu") (at {pos[0]} {pos[1]}) '
        f'(fp_text reference "{ref}" (at {ref_local[0]} {ref_local[1]}) (layer "F.SilkS") '
        "(effects (font (size 1 1) (thickness 0.15)))) "
        f"{pads} {court})"
    )


def _write(tmp_path: Path, *footprints: str) -> Path:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(HEADER + "\n".join(footprints) + ")\n")
    return path


def test_npth_hole_blocks_reference(tmp_path: Path):
    """A reference sitting on an NPTH mounting hole is not 'already clear'."""
    npth = '(pad "" np_thru_hole circle (at 0 0) (size 3 3) (drill 3) (layers "*.Cu" "*.Mask"))'
    hole = _fp("H1", (20, 20), (0, 3), pads=npth, half=(1.6, 1.6))
    # J1's label starts exactly on H1's hole.
    j1 = _fp("J1", (20, 30), (0, -10), half=(0.5, 0.3))
    placer = SilkRefPlacer(_write(tmp_path, hole, j1))
    result = placer.plan(clearance_mm=0.15)
    by_ref = {p.footprint_ref: p for p in result.placements}
    assert by_ref["J1"].status == "moved"
    hole_geom = [g for g, lbl in placer._render_context["apertures"]["F"] if "NPTH" in lbl]
    assert hole_geom, "NPTH pads must be obstacles"
    # The chosen position must not intersect the hole.
    import math

    nx, ny = by_ref["J1"].new_position
    assert math.hypot(nx - 20, ny - 20) > 1.5


def test_label_prefers_own_part_over_neighbour(tmp_path: Path):
    """Dense pair: each label ends nearer its own courtyard by >= margin or is flagged."""
    parts = []
    for i in range(6):
        x = 10 + i * 1.6
        parts.append(_fp(f"C{i}", (x, 20), (0, 0), half=(0.5, 0.3)))
    placer = SilkRefPlacer(_write(tmp_path, *parts))
    result = placer.plan(clearance_mm=0.15)
    assert result.placements
    for p in result.placements:
        assert p.status in {"moved", "unchanged"}
        assert p.association_margin_mm is not None
        if p.association_margin_mm < ASSOCIATION_MARGIN_MM:
            assert p.low_association  # reported tie, never silent
        else:
            assert not p.low_association
        assert p.association_margin_mm > 0 or p.low_association


def test_isolated_label_has_no_low_association(tmp_path: Path):
    placer = SilkRefPlacer(_write(tmp_path, _fp("R1", (30, 20), (0, -1.5))))
    (p,) = placer.plan(clearance_mm=0.15).placements
    assert p.status == "unchanged"
    assert not p.low_association
