"""No-Shapely outline fallback in ``kct optimize-placement`` (#7148).

Without Shapely, ``_read_board_data`` used to read only Edge.Cuts ``gr_line``
segments, so a single ``gr_rect`` outline (board 04) fell back to a
footprints+10 mm box that is larger than the real board.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from kicad_tools.optim.board_outline import extract_board_outline, to_origin_relative
from kicad_tools.placement.geometry import extract_board_outline as placement_outline
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_04 = REPO_ROOT / "boards" / "04-stm32-devboard" / "output" / "stm32_devboard.kicad_pcb"
EDGE = (118.5, 67.5, 178.5, 107.5)


def _saved_positions(path: Path) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for child in parse_file(path).iter_children():
        if child.tag != "footprint":
            continue
        at = child.find("at")
        ref = None
        for prop in child.find_all("property"):
            if prop.get_string(0) == "Reference":
                ref = prop.get_string(1)
        for t in child.find_all("fp_text"):
            if t.get_string(0) == "reference":
                ref = t.get_string(1)
        out[ref] = (at.get_float(0), at.get_float(1))
    return out


@pytest.mark.skipif(not BOARD_04.exists(), reason="board 04 fixture not present")
def test_no_shapely_board04_parts_stay_inside_real_edge(tmp_path, monkeypatch) -> None:
    from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

    monkeypatch.setattr("kicad_tools.pcb.board_geometry.has_shapely", lambda: False)
    src = tmp_path / BOARD_04.name
    shutil.copyfile(BOARD_04, src)
    out = tmp_path / "out.kicad_pcb"

    from kicad_tools.cli.optimize_placement_cmd import _read_board_data

    outline = _read_board_data(str(src))[2]
    assert (outline.min_x, outline.min_y, outline.max_x, outline.max_y) == pytest.approx(
        (0.0, 0.0, 60.0, 40.0)
    )
    rc = run_optimize_placement(
        str(src),
        strategy_name="cmaes",
        max_iterations=30,
        output_path=str(out),
        quiet=True,
        allow_infeasible=True,
    )
    assert rc == 0
    pos = _saved_positions(out)
    assert len(pos) == 17
    outside = [
        r
        for r, (x, y) in pos.items()
        if not (EDGE[0] - 1e-6 <= x <= EDGE[2] + 1e-6 and EDGE[1] - 1e-6 <= y <= EDGE[3] + 1e-6)
    ]
    assert outside == [], f"parts outside real Edge.Cuts: {outside}"


def _pcb(tmp_path: Path, edge_body: str, origin_shift: bool = True) -> PCB:
    text = (
        '(kicad_pcb (version 20241229) (generator "t")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "")\n' + edge_body + "\n)\n"
    )
    p = tmp_path / "t.kicad_pcb"
    p.write_text(text)
    return PCB.load(str(p))


POLY = (
    "(gr_poly (pts (xy 10 20) (xy 50 20) (xy 50 40) (xy 10 40)) "
    '(layer "Edge.Cuts") (width 0.1) (fill none))'
)
# Quarter-circle arc, centre (10,20)->start (30,20), mid, end (10,40)
ARC_BOARD = (
    '(gr_line (start 10 20) (end 30 20) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_arc (start 30 20) (mid 24.142 34.142) (end 10 40) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_line (start 10 40) (end 10 20) (layer "Edge.Cuts") (width 0.1))'
)


def _bbox(poly):
    xs = [v.x for v in poly.vertices]
    ys = [v.y for v in poly.vertices]
    return min(xs), min(ys), max(xs), max(ys)


def test_gr_poly_outline(tmp_path) -> None:
    b = _bbox(extract_board_outline(_pcb(tmp_path, POLY)))
    assert b == pytest.approx((10, 20, 50, 40))


def test_gr_arc_outline_includes_arc_extremes(tmp_path) -> None:
    # Arc from (30,20) via (24.142,34.142) to (10,40) is centred at (10,20)
    # radius 20; sweeps through (10+20cos45, 20+20sin45) -> max x is 30 (start).
    b = _bbox(extract_board_outline(_pcb(tmp_path, ARC_BOARD)))
    assert b == pytest.approx((10, 20, 30, 40), abs=0.05)


def test_arc_bulge_extends_bbox(tmp_path) -> None:
    # Semicircle (0,0)->(0,20) bulging to x=10 via mid (10,10), plus closing line.
    body = (
        '(gr_arc (start 0 0) (mid 10 10) (end 0 20) (layer "Edge.Cuts") (width 0.1))\n'
        '(gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.1))'
    )
    b = _bbox(extract_board_outline(_pcb(tmp_path, body)))
    assert b == pytest.approx((0, 0, 10, 20), abs=0.05)


@pytest.mark.parametrize(
    "body,expected",
    [
        (POLY, (10, 20, 50, 40)),
        (
            '(gr_rect (start 10 20) (end 50 40) (layer "Edge.Cuts") (width 0.1))',
            (10, 20, 50, 40),
        ),
    ],
)
def test_placement_geometry_delegates_origin_relative(tmp_path, body, expected) -> None:
    pcb = _pcb(tmp_path, body)
    ox, oy = pcb.board_origin
    assert (ox, oy) != (0.0, 0.0)
    assert _bbox(to_origin_relative(extract_board_outline(pcb), pcb)) == pytest.approx(
        (expected[0] - ox, expected[1] - oy, expected[2] - ox, expected[3] - oy)
    )
    o = placement_outline(pcb)
    assert (o.min_x, o.min_y, o.max_x, o.max_y) == pytest.approx(
        (expected[0] - ox, expected[1] - oy, expected[2] - ox, expected[3] - oy)
    )


def test_placement_geometry_arc_origin_relative(tmp_path) -> None:
    pcb = _pcb(tmp_path, ARC_BOARD)
    ox, oy = pcb.board_origin
    o = placement_outline(pcb)
    assert (o.min_x, o.min_y, o.max_x, o.max_y) == pytest.approx(
        (10 - ox, 20 - oy, 30 - ox, 40 - oy), abs=0.05
    )


def test_no_outline_warns_loudly(tmp_path, caplog) -> None:
    pcb = _pcb(tmp_path, "")
    with caplog.at_level(logging.WARNING):
        o = placement_outline(pcb)
    assert (o.min_x, o.max_x) == (0.0, 100.0)
    assert any("Edge.Cuts" in r.message and "falling back" in r.message for r in caplog.records)


def test_require_outline_refuses_and_default_warns(tmp_path, capsys) -> None:
    from kicad_tools.cli.optimize_placement_cmd import _read_board_data

    src = tmp_path / "t.kicad_pcb"
    _pcb(tmp_path, "")
    with pytest.raises(ValueError, match="require-outline"):
        _read_board_data(str(src), require_outline=True)
    _read_board_data(str(src))
    assert "No Edge.Cuts board outline" in capsys.readouterr().err


def _line(x1, y1, x2, y2) -> str:
    return f'(gr_line (start {x1} {y1}) (end {x2} {y2}) (layer "Edge.Cuts") (width 0.1))'


OUTER_LINES = "\n".join(
    [_line(0, 0, 100, 0), _line(100, 0, 100, 80), _line(100, 80, 0, 80), _line(0, 80, 0, 0)]
)
CUTOUT_RECT = '(gr_rect (start 40 30) (end 60 50) (layer "Edge.Cuts") (width 0.1) (fill none))'
CUTOUT_POLY = (
    "(gr_poly (pts (xy 40 30) (xy 60 30) (xy 60 50) (xy 40 50)) "
    '(layer "Edge.Cuts") (width 0.1) (fill none))'
)


@pytest.mark.parametrize("cutout", [CUTOUT_RECT, CUTOUT_POLY])
@pytest.mark.parametrize("cutout_first", [True, False])
def test_line_outline_beats_cutout(tmp_path, cutout, cutout_first) -> None:
    body = cutout + "\n" + OUTER_LINES if cutout_first else OUTER_LINES + "\n" + cutout
    b = _bbox(extract_board_outline(_pcb(tmp_path, body)))
    assert b == pytest.approx((0, 0, 100, 80))
