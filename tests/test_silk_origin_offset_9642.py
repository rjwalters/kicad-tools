"""Board-level silk lines/arcs share the pads' frame on origin-offset boards (Issue #9642).

``PCB.load`` shifts footprints (and so pads) into the board-relative frame and
records the shift in ``pcb._board_origin``; board-level ``gr_line`` / ``gr_arc``
keep the file's sheet-absolute coordinates.  ``silk_pad_clearance`` (and the
pin-1 marker rule) used to compare the two frames directly.  Follow-up to #8561.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.factory_clearance import check_silk_pad_clearance  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_silk_overlap,
    check_silkscreen_line_width,
    iter_unmodeled_silk_graphics,
)

# Board-relative design: 60x40 board, TH pad J1-1 at board (10, 10).
# Board-level silk is given in *board-relative* coordinates (``on_pad`` on the
# pad, ``clear`` 30 mm away) and offset into the file frame by the origin.
_TEMPLATE = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (44 "Edge.Cuts" user))
  (net 0 "")
  (gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "Connector:J" (layer "F.Cu") (at {jx} {jy})
    (property "Reference" "J1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 2 2) (drill 1)
      (layers "*.Cu" "*.Mask")))
{extra}
)
"""


def _line(x0: float, y0: float, x1: float, y1: float, layer: str = "F.SilkS") -> str:
    return (
        f"  (gr_line (start {x0} {y0}) (end {x1} {y1}) "
        f'(stroke (width 0.15) (type solid)) (layer "{layer}"))'
    )


def _arc(x0: float, y0: float, mx: float, my: float, x1: float, y1: float) -> str:
    return (
        f"  (gr_arc (start {x0} {y0}) (mid {mx} {my}) (end {x1} {y1}) "
        f'(stroke (width 0.15) (type solid)) (layer "F.SilkS"))'
    )


def _board(tmp_path: Path, ox: float, oy: float, extra_fn, name: str) -> PCB:
    extra = "\n".join(extra_fn(ox, oy))
    text = _TEMPLATE.format(
        x0=ox, y0=oy, x1=ox + 60, y1=oy + 40, jx=ox + 10, jy=oy + 10, extra=extra
    )
    path = tmp_path / f"{name}_{int(ox)}_{int(oy)}.kicad_pcb"
    path.write_text(text)
    pcb = PCB.load(path)
    assert pcb._board_origin == (ox, oy)
    return pcb


def _rules():
    return get_profile("jlcpcb-tier1").get_design_rules(layers=2)


def _pad_clearance(pcb: PCB):
    return check_silk_pad_clearance(pcb, _rules()).violations


def _on_pad(ox: float, oy: float):
    # Board-relative (9..11, 10) crosses the pad at (10, 10).
    return [_line(ox + 9, oy + 10, ox + 11, oy + 10)]


def _false_spot(ox: float, oy: float):
    # File coordinates equal to the pad's *board* position: only on the pad if
    # the buggy rule forgot to subtract the origin.  Truly in open board.
    return [_line(9, 10, 11, 10)]


@pytest.mark.parametrize("origin", [(0.0, 0.0), (100.0, 100.0), (50.5, 20.25)])
def test_line_on_pad_is_flagged(tmp_path: Path, origin: tuple[float, float]):
    found = _pad_clearance(_board(tmp_path, *origin, _on_pad, "on"))
    assert [v.items[1] for v in found] == ["J1-1"]
    assert found[0].severity == "error"


@pytest.mark.parametrize("origin", [(100.0, 100.0), (50.5, 20.25)])
def test_line_clear_of_pads_not_flagged(tmp_path: Path, origin: tuple[float, float]):
    assert _pad_clearance(_board(tmp_path, *origin, _false_spot, "clear")) == []


def test_arc_on_pad_flagged_and_arc_clear_not(tmp_path: Path):
    def on(ox, oy):
        return [_arc(ox + 9, oy + 10, ox + 10, oy + 9, ox + 11, oy + 10)]

    def clear(ox, oy):
        return [_arc(9, 10, 10, 9, 11, 10)]

    flagged = _pad_clearance(_board(tmp_path, 100, 100, on, "arc_on"))
    assert [v.items[1] for v in flagged] == ["J1-1"]
    assert _pad_clearance(_board(tmp_path, 100, 100, clear, "arc_clear")) == []


def test_back_silk_side(tmp_path: Path):
    def on(ox, oy):
        return [_line(ox + 9, oy + 10, ox + 11, oy + 10, "B.SilkS")]

    def clear(ox, oy):
        return [_line(9, 10, 11, 10, "B.SilkS")]

    assert [v.items[1] for v in _pad_clearance(_board(tmp_path, 100, 100, on, "b_on"))] == ["J1-1"]
    assert _pad_clearance(_board(tmp_path, 100, 100, clear, "b_clear")) == []


def test_offset_board_matches_origin_zero_twin(tmp_path: Path):
    def both(ox, oy):
        return _on_pad(ox, oy) + [_line(ox + 30, oy + 20, ox + 32, oy + 20)]

    def summary(pcb: PCB):
        return sorted(
            (v.rule_id, v.layer, tuple(v.items), tuple(round(c, 3) for c in v.location))
            for v in _pad_clearance(pcb)
        )

    twin = summary(_board(tmp_path, 0, 0, both, "twin"))
    assert twin
    assert summary(_board(tmp_path, 100, 100, both, "off")) == twin


def test_location_agrees_with_silk_overlap(tmp_path: Path):
    """The same board-level silk item reports one location from both rules."""

    def extra(ox, oy):
        # Two collinear overlapping silk lines lying on the pad.
        return [
            _line(ox + 9, oy + 10, ox + 11, oy + 10),
            _line(ox + 9.5, oy + 10, ox + 11, oy + 10),
        ]

    pcb = _board(tmp_path, 100, 100, extra, "loc")
    pad_locs = {tuple(round(c, 3) for c in v.location) for v in _pad_clearance(pcb)}
    overlap = check_silk_overlap(pcb, _rules()).violations
    overlap_locs = {tuple(round(c, 3) for c in v.location) for v in overlap}
    assert pad_locs and overlap_locs
    assert pad_locs & overlap_locs, (pad_locs, overlap_locs)
    # Both are in the board-relative frame, not the file's.
    for x, y in pad_locs | overlap_locs:
        assert 0 <= x <= 60 and 0 <= y <= 40


def test_line_width_location_is_board_frame(tmp_path: Path):
    def extra(ox, oy):
        return [_line(ox + 20, oy + 20, ox + 22, oy + 20).replace("width 0.15", "width 0.05")]

    pcb = _board(tmp_path, 100, 100, extra, "width")
    widths = check_silkscreen_line_width(pcb, _rules()).violations
    assert [tuple(round(c, 3) for c in v.location) for v in widths] == [(20.0, 20.0)]


def test_unmodeled_silk_location_is_board_frame(tmp_path: Path, monkeypatch):
    # Narrow the modeled set so a plain gr_line counts as "unmodeled".
    from kicad_tools.validate.rules import silkscreen

    monkeypatch.setattr(silkscreen, "MODELED_SILK_GRAPHIC_TYPES", ("rect",))
    pcb = _board(
        tmp_path, 100, 100, lambda ox, oy: [_line(ox + 20, oy + 20, ox + 22, oy + 20)], "unm"
    )
    found = list(iter_unmodeled_silk_graphics(pcb))
    assert [tuple(round(c, 3) for c in loc) for _l, _k, loc, _y, _u in found] == [(20.0, 20.0)]


def test_pin1_marker_board_silk_uses_board_frame():
    """Board-level pin-1 dot is matched against footprints in the board frame."""
    from kicad_tools.schema.pcb import BoardGraphic  # noqa: PLC0415

    from .test_pin1_marker import _fp, _pcb, _run  # noqa: PLC0415

    def dot(dx: float) -> BoardGraphic:
        return BoardGraphic(
            graphic_type="circle",
            layer="F.SilkS",
            stroke_width=0.12,
            center=(47.8 + dx, 48.5 + dx),
            end=(47.95 + dx, 48.5 + dx),
        )

    # Same dot as ``test_board_level_silk_marker_ok``, expressed in file
    # coordinates of a board whose origin is (100, 100): still a marker.
    pcb = _pcb(_fp(), board_graphics=[dot(100.0)])
    pcb._board_origin = (100.0, 100.0)
    assert _run(pcb) == []
    # An unshifted dot is 100 mm away in the board frame: no marker.
    pcb = _pcb(_fp(), board_graphics=[dot(0.0)])
    pcb._board_origin = (100.0, 100.0)
    assert len(_run(pcb)) == 1
