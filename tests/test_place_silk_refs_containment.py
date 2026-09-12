"""Reference placement must remain on board material, including cutouts."""

from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from kicad_tools.schema.pcb import PCB
from kicad_tools.silkscreen.place_refs import SilkRefPlacer, _oriented_text_geometry


def _rect(x0, y0, x1, y1):
    return f'(gr_rect (start {x0} {y0}) (end {x1} {y1}) (layer "Edge.Cuts") (width .05))'


def _board(tmp_path: Path, edges: str, *, position=(1, 10), keepout=False):
    obstacle = (
        """
      (footprint "Keepout" (layer "F.Cu") (at 0 0)
        (fp_rect (start 0 0) (end 100 60) (layer "F.CrtYd")
          (stroke (width .05) (type solid)) (fill none)))
    """
        if keepout
        else ""
    )
    path = tmp_path / "containment.kicad_pcb"
    path.write_text(f"""(kicad_pcb (version 20240108) (generator test)
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user))
      {edges}
      (footprint "Test" (layer "F.Cu") (at {position[0]} {position[1]})
        (fp_text reference "R1" (at 0 0) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness .15)))))
      {obstacle})""")
    return path


def _envelope(placement):
    return _oriented_text_geometry(
        "R1", (1, 1), 0.15, placement.new_position, placement.new_rotation
    )


def test_impossible_edge_placement_cannot_escape_board(tmp_path):
    path = _board(tmp_path, _rect(0, 0, 100, 60), keepout=True)
    before = path.read_bytes()
    placer = SilkRefPlacer(path)
    result = placer.plan(max_offset_mm=3)
    ref = result.placements[0]
    assert ref.status == "unplaceable"
    assert ref.new_position == ref.old_position
    assert placer.apply(result) == 0
    assert path.read_bytes() == before


def test_reference_in_cutout_moves_onto_material(tmp_path):
    path = _board(tmp_path, _rect(0, 0, 100, 60) + _rect(8, 8, 12, 12), position=(10, 10))
    placer = SilkRefPlacer(path)
    result = placer.plan(edge_clearance_mm=0.5)
    ref = result.placements[0]
    material = box(0, 0, 100, 60).difference(box(8, 8, 12, 12))
    assert ref.status == "moved"
    assert material.covers(_envelope(ref))
    assert material.boundary.distance(_envelope(ref)) >= 0.5 - 1e-4
    placer.apply(result)
    placer.save()
    reparsed = SilkRefPlacer(path).plan(edge_clearance_mm=0.5).placements[0]
    assert reparsed.status == "unchanged"
    assert material.covers(_envelope(reparsed))


def test_nested_island_remains_material(tmp_path):
    path = _board(
        tmp_path,
        _rect(0, 0, 100, 60) + _rect(10, 10, 30, 30) + _rect(15, 15, 25, 25),
        position=(20, 20),
    )
    ref = SilkRefPlacer(path).plan().placements[0]
    assert ref.status == "unchanged"


def test_shuffled_line_segments_with_small_closing_gap(tmp_path):
    edges = """
      (gr_line (start 100 60) (end 0 60) (layer "Edge.Cuts") (width .05))
      (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (width .05))
      (gr_line (start 0 60) (end 0 .001) (layer "Edge.Cuts") (width .05))
      (gr_line (start 100 0) (end 100 60) (layer "Edge.Cuts") (width .05))
    """
    path = _board(tmp_path, edges, position=(10, 10))
    assert SilkRefPlacer(path).plan().placements[0].status == "unchanged"


def test_polygon_outline_and_nonzero_origin(tmp_path):
    edges = '(gr_poly (pts (xy 100 100) (xy 120 100) (xy 120 110) (xy 110 110) (xy 110 120) (xy 100 120)) (layer "Edge.Cuts") (width .05))'
    path = _board(tmp_path, edges, position=(115, 115))
    placer = SilkRefPlacer(path)
    ref = placer.plan().placements[0]
    ox, oy = placer.pcb._board_origin
    material = Polygon(
        [
            (100 - ox, 100 - oy),
            (120 - ox, 100 - oy),
            (120 - ox, 110 - oy),
            (110 - ox, 110 - oy),
            (110 - ox, 120 - oy),
            (100 - ox, 120 - oy),
        ]
    )
    assert ref.status == "moved"
    assert material.covers(_envelope(ref))


@pytest.mark.parametrize(
    "edges",
    [
        "",
        '(gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (width .05))',
        _rect(0, 0, 100, 60) + '(gr_line (start 5 5) (end 6 5) (layer "Edge.Cuts") (width .05))',
        _rect(0, 0, 100, 60) + _rect(90, 30, 110, 70),
        '(gr_poly (pts (xy 0 0) (xy 20 20) (xy 0 20) (xy 20 0)) (layer "Edge.Cuts") (width .05))',
        _rect(0, 0, 100, 60)
        + '(gr_circle (center 10 10) (end 12 10) (layer "Edge.Cuts") (width .05))',
        _rect(0, 0, 100, 60)
        + '(gr_arc (start 8 10) (mid 10 8) (end 12 10) (layer "Edge.Cuts") (width .05))',
    ],
)
def test_unavailable_outline_is_reported_without_moving(tmp_path, edges):
    path = _board(tmp_path, edges)
    placer = SilkRefPlacer(path)
    ref = placer.plan().placements[0]
    assert ref.status == "unplaceable"
    assert ref.new_position == ref.old_position
    assert "board outline unavailable" in ref.reason


@pytest.mark.parametrize(
    "geometry",
    [
        '(footprint "Cutout" (layer "F.Cu") (at 10 10) (fp_rect (start -2 -2) (end 2 2) (layer "Edge.Cuts") (stroke (width .05) (type solid))))',
        '(gr_rect (start 0 0) (end 100) (layer "Edge.Cuts") (width .05))',
        '(gr_line (start 0 0) (layer "Edge.Cuts") (width .05))',
    ],
)
def test_unsupported_or_malformed_additional_edges_cannot_be_ignored(tmp_path, geometry):
    path = _board(tmp_path, _rect(0, 0, 100, 60) + geometry)
    ref = SilkRefPlacer(path).plan().placements[0]
    assert ref.status == "unplaceable"
    assert "board outline unavailable" in ref.reason


@pytest.mark.parametrize(
    "edge",
    [
        '(gr_rect (start 100 100) (end 120) (layer "Edge.Cuts") (width .05))',
        '(gr_line (start 100 100) (layer "Edge.Cuts") (width .05))',
    ],
)
def test_malformed_outline_is_diagnostic_only_for_silkscreen(tmp_path, edge):
    path = _board(tmp_path, _rect(100, 100, 120, 120) + edge, position=(110, 110))
    before = path.read_bytes()
    # Ordinary PCB.load() tolerates a malformed outline (falls back to a
    # (0, 0) origin), matching its pre-existing behavior for other callers
    # such as relocate_in_pad_vias (see test_fix_vias.py). Only the
    # silkscreen consumer below treats it as diagnostic-worthy.
    assert PCB.load(path)._board_origin == (0.0, 0.0)
    placer = SilkRefPlacer(path)
    result = placer.plan()
    ref = result.placements[0]
    assert ref.status == "unplaceable"
    assert ref.old_position == ref.new_position == (110, 110)
    assert "board outline unavailable" in ref.reason
    assert placer.apply(result) == 0
    assert path.read_bytes() == before
