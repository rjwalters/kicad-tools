"""Legacy rounded outlines must survive contour editing and placement (#4884)."""

import pytest

from kicad_tools.optim.placement import PlacementOptimizer
from kicad_tools.schema import PCB
from kicad_tools.schema.pcb import BoardGraphic, FootprintGraphic, GraphicArc
from tests.test_corpus_readiness import board_text


@pytest.fixture(params=["rounded-v6", "rounded-legacy-arc", "rounded-legacy-arc-mixed-sign"])
def rounded_board(request, tmp_path):
    path = tmp_path / "rounded.kicad_pcb"
    path.write_text(board_text(outline=request.param))
    return PCB.load(path)


def test_contour_is_one_outline(rounded_board):
    contours = rounded_board.list_edge_contours()
    assert len(contours) == 1
    assert contours[0].element_count == 8
    assert contours[0].bbox == pytest.approx((0, 0, 20, 10))
    assert not contours[0].is_mounting_hole


def test_replace_outline_leaves_no_corner_arcs(rounded_board, tmp_path):
    assert rounded_board.replace_outline(0, 0, 30, 20) == 1
    saved = tmp_path / "replaced.kicad_pcb"
    rounded_board.save(saved)
    reloaded = PCB.load(saved)
    assert not reloaded._sexp.find_all("gr_arc")
    contours = reloaded.list_edge_contours()
    assert len(contours) == 1
    assert contours[0].element_count == 4
    assert contours[0].bbox == pytest.approx((0, 0, 30, 20))


def test_graphic_views_and_placement_share_on_arc_points(rounded_board):
    for node in rounded_board._sexp.find_all("gr_arc"):
        arc = GraphicArc.from_sexp(node)
        board = BoardGraphic.from_sexp(node, "arc")
        footprint = FootprintGraphic.from_sexp(node, "arc")
        assert board.start == pytest.approx(arc.start)
        assert board.end == pytest.approx(arc.end)
        assert footprint.start == pytest.approx(arc.start)
        assert footprint.mid == pytest.approx(arc.mid)
        assert footprint.end == pytest.approx(arc.end)
        chords = PlacementOptimizer._linearize_arc(node)
        assert len(chords) == 16
        assert chords[0][0] == pytest.approx(arc.start, abs=1e-6)
        assert chords[-1][1] == pytest.approx(arc.end, abs=1e-6)
        assert chords[7][1] == pytest.approx(arc.mid, abs=1e-6)


def test_replace_preserves_real_mounting_hole(rounded_board, tmp_path):
    from kicad_tools.sexp import parse_string

    rounded_board._sexp.append(
        parse_string('(gr_circle (center 10 5) (end 11 5) (layer "Edge.Cuts") (width 0.05))')
    )
    assert rounded_board.replace_outline(0, 0, 30, 20) == 1
    saved = tmp_path / "with-hole.kicad_pcb"
    rounded_board.save(saved)
    reloaded = PCB.load(saved)
    assert len(reloaded._sexp.find_all("gr_circle")) == 1
    assert not reloaded._sexp.find_all("gr_arc")
    assert sorted(c.is_mounting_hole for c in reloaded.list_edge_contours()) == [False, True]


def test_legacy_arc_bounds_use_on_arc_points_without_rewriting_source():
    from kicad_tools.sexp import parse_string, serialize_sexp

    node = parse_string(
        '(gr_arc (start 10 10) (end 20 10) (angle 180) (layer "Edge.Cuts") (width 0.05))'
    )
    original = serialize_sexp(node)
    points = PCB._collect_edge_points([node])
    assert len(points) == 3
    assert min(p[0] for p in points) == pytest.approx(0)
    assert max(p[1] for p in points) == pytest.approx(20)
    graphic = BoardGraphic.from_sexp(node, "arc")
    assert graphic.start == pytest.approx((20, 10))
    assert graphic.mid == pytest.approx((10, 20))
    assert graphic.end == pytest.approx((0, 10))
    PlacementOptimizer._linearize_arc(node)
    assert serialize_sexp(node) == original
